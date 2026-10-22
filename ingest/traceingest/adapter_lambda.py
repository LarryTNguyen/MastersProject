"""Dataset Adapter Lambda.

SQS hands us S3 "object created" events. For each new raw file: detect the
format, convert to spans, write canonical JSONL to normalized/ for the
reconstruction step.

Env: NORMALIZED_BUCKET, NORMALIZED_PREFIX (default normalized/), BASE_EPOCH_US,
TRACE_RUN_ID.

Each span gets sta.normalized_us so the dashboard can measure adapter lag.
Retries are safe: same input key -> same output key, so nothing gets duplicated.
Bad files are reported per SQS message (ReportBatchItemFailures), so one bad
file does not retry the whole batch.
"""
from __future__ import annotations

import io
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

from . import formats
from .canonical import NormalizationContext, write_jsonl as write_canonical_jsonl
from .spans import validate

_s3 = None


def _client():
    global _s3
    if _s3 is None:
        import boto3  # provided by the Lambda Python runtime
        _s3 = boto3.client("s3")
    return _s3


def output_key(raw_key: str, prefix: str) -> str:
    stem, _ = os.path.splitext(raw_key)
    return f"{prefix.rstrip('/')}/{stem}.jsonl"


def normalize_object(s3, bucket: str, key: str, out_bucket: str, out_prefix: str,
                     base_epoch_us: int = 0, received_at_unix_ns: int = 0) -> dict:
    obj = s3.get_object(Bucket=bucket, Key=key)
    data = obj["Body"].read()
    head = formats.first_bytes_text(data)
    if head is None:
        raise ValueError("Parquet uploads are not supported in the Lambda; convert to CSV or OTLP JSON first")
    fmt = formats.detect_format(head, key)
    result = formats.load_text(io.StringIO(data.decode("utf-8", errors="replace")), fmt=fmt,
                               name=key, base_epoch_us=base_epoch_us)
    good = []
    invalid = 0
    normalized_us = int(time.time() * 1_000_000)
    for s in result.spans:
        if validate(s):
            invalid += 1
        else:
            s.attributes["sta.normalized_us"] = normalized_us
            good.append(s)
    buf = io.StringIO()
    run_id = os.environ.get("TRACE_RUN_ID")
    context = NormalizationContext.for_source(f"s3://{bucket}/{key}", run_id=run_id,
                                              received_at_unix_ns=received_at_unix_ns)
    write_canonical_jsonl(good, buf, context)
    out_key = output_key(key, out_prefix)
    s3.put_object(Bucket=out_bucket, Key=out_key, Body=buf.getvalue().encode("utf-8"),
                  ContentType="application/x-ndjson",
                  Metadata={"source-key": key[:1000], "format": fmt, "spans": str(len(good)),
                            "schema-version": "1.0.0", "run-id": context.run_id})
    return {"input": f"s3://{bucket}/{key}", "output": f"s3://{out_bucket}/{out_key}",
            "format": fmt, "spans": len(good), "invalid_dropped": invalid, "stats": result.stats}


def _s3_records(message_body: str):
    event = json.loads(message_body)
    if event.get("Event") == "s3:TestEvent":
        return []
    return event.get("Records", [])


def _event_time_ns(value: Optional[str]) -> int:
    """Convert an S3 event's RFC3339 timestamp to epoch nanoseconds."""
    if not value:
        return 0
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("S3 eventTime must include a timezone")
    delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return ((delta.days * 86400 + delta.seconds) * 1_000_000_000
            + delta.microseconds * 1000)


def handler(event, context=None, s3=None):
    s3 = s3 or _client()
    out_prefix = os.environ.get("NORMALIZED_PREFIX", "normalized/")
    out_bucket_env: Optional[str] = os.environ.get("NORMALIZED_BUCKET")
    base_epoch_us = int(os.environ.get("BASE_EPOCH_US", "0"))
    failures, results = [], []
    for msg in event.get("Records", []):
        try:
            for rec in _s3_records(msg["body"]):
                bucket = rec["s3"]["bucket"]["name"]
                key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
                if key.startswith(out_prefix):
                    continue        # never re-process our own output
                r = normalize_object(s3, bucket, key, out_bucket_env or bucket, out_prefix, base_epoch_us,
                                     _event_time_ns(rec.get("eventTime")))
                results.append(r)
                print(json.dumps({"msg": "normalized", **{k: v for k, v in r.items() if k != "stats"}}))
        except Exception as e:  # report this message as failed, keep going with the rest
            print(json.dumps({"msg": "failed", "messageId": msg.get("messageId"), "error": repr(e)}))
            failures.append({"itemIdentifier": msg.get("messageId")})
    return {"batchItemFailures": failures, "results": results}
