"""Collector Lambda: the live path's front door, behind a Function URL.

An instrumented application (or `traceingest replay --url <Function URL>`) POSTs an
OTLP/HTTP JSON export to it. The collector does as little as possible, so exporting
traces never waits on our analysis:

1. checks `Authorization: Bearer <INGEST_TOKEN>` (constant-time compare);
2. checks the request is OTLP JSON and not too big;
3. stores the body as-is at s3://RAW_BUCKET/RAW_PREFIX/YYYY/MM/DD/<id>.json;
4. answers 200 with an empty ExportTraceServiceResponse.

No parsing happens here. The S3 "object created" event goes through SQS to the
dataset adapter Lambda, the same as an upload, which stamps when the object arrived
(sta.received_us) and when it was normalized (sta.normalized_us).

Env:
  RAW_BUCKET     bucket for raw batches (required)
  RAW_PREFIX     key prefix, default live/  (needs its own S3 event notification to the
                 adapter's queue; must not overlap normalized/)
  INGEST_TOKEN   shared secret the senders put in the Authorization header. When it
                 is not set the collector rejects everything (fails closed).
  MAX_BYTES      largest accepted body, default 5 MB (Function URLs stop at 6 MB)

Handler: traceingest.collector_lambda.handler (ships in the adapter zip,
tools/build_lambda.py). OTLP protobuf is answered with 415: the adapter reads JSON.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import time
import uuid

_s3 = None
PATHS = ("/v1/traces", "/")


def _client():
    global _s3
    if _s3 is None:
        import boto3  # provided by the Lambda Python runtime
        _s3 = boto3.client("s3")
    return _s3


def _reply(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _authorized(headers: dict, token: str) -> bool:
    got = headers.get("authorization", "")
    return bool(token) and hmac.compare_digest(got.encode(), f"Bearer {token}".encode())


def raw_key(prefix: str, now: float, ident: str) -> str:
    return f"{prefix.rstrip('/')}/{time.strftime('%Y/%m/%d', time.gmtime(now))}/{ident}.json"


def handler(event, context=None, s3=None, now=time.time):
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method", event.get("httpMethod", "POST")).upper()
    path = http.get("path", event.get("rawPath", "/")) or "/"
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    if path.rstrip("/") not in {p.rstrip("/") for p in PATHS}:
        return _reply(404, {"error": "POST OTLP JSON to /v1/traces"})
    if method != "POST":
        return _reply(405, {"error": "use POST"})
    if not _authorized(headers, os.environ.get("INGEST_TOKEN", "")):
        return _reply(401, {"error": "missing or wrong bearer token"})
    ctype = headers.get("content-type", "application/json").split(";")[0].strip().lower()
    if ctype != "application/json":
        return _reply(415, {"error": "send OTLP as JSON (application/json); protobuf is not supported"})

    body = event.get("body") or ""
    data = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")
    if not data:
        return _reply(400, {"error": "empty body"})
    if len(data) > int(os.environ.get("MAX_BYTES", str(5 * 1024 * 1024))):
        return _reply(413, {"error": "batch too large; send smaller batches"})

    key = raw_key(os.environ.get("RAW_PREFIX", "live/"), now(), uuid.uuid4().hex)
    (s3 or _client()).put_object(Bucket=os.environ["RAW_BUCKET"], Key=key, Body=data,
                                 ContentType="application/json")
    print(json.dumps({"msg": "stored", "key": key, "bytes": len(data)}))
    return _reply(200, {"partialSuccess": {}})
