"""Upload path: put a trace file into the raw S3 bucket with a presigned URL.

The browser (and this CLI) never sends trace files through our API: the
dashboard API hands out a short-lived presigned PUT URL and the file goes
straight to S3. The S3 event then flows through SQS to the Dataset Adapter.

Three ways to get the URL:
  * ``--presigned-url``: paste one you already have.
  * ``--presign-endpoint``: ask the dashboard API for one (POST JSON
    ``{"filename", "content_type"}``, expects ``{"url": ..., "key": ...}``).
    That endpoint is part of the dashboard Lambda, so this contract is a draft.
  * ``--bucket/--key``: sign it yourself with your own AWS credentials (boto3).
"""
from __future__ import annotations

import json
import mimetypes
import os
import urllib.request
from typing import Optional, Tuple

DEFAULT_EXPIRES_S = 900


def content_type_for(path: str) -> str:
    if path.endswith((".json", ".jsonl")):
        return "application/json"
    if path.endswith(".csv"):
        return "text/csv"
    if path.endswith(".parquet"):
        return "application/octet-stream"
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def presign_with_boto3(bucket: str, key: str, content_type: str,
                       expires_s: int = DEFAULT_EXPIRES_S, region: Optional[str] = None) -> str:
    import boto3  # only needed for self-signing
    s3 = boto3.client("s3", region_name=region)
    return s3.generate_presigned_url(
        "put_object", Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expires_s)


def presign_from_api(endpoint: str, filename: str, content_type: str,
                     token: Optional[str] = None, opener=urllib.request.urlopen) -> Tuple[str, Optional[str]]:
    body = json.dumps({"filename": filename, "content_type": content_type}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    with opener(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return data["url"], data.get("key")


def put_file(url: str, path: str, content_type: str, opener=urllib.request.urlopen) -> int:
    with open(path, "rb") as fh:
        data = fh.read()
    req = urllib.request.Request(url, data=data, method="PUT",
                                 headers={"Content-Type": content_type, "Content-Length": str(len(data))})
    with opener(req, timeout=120) as resp:
        if not 200 <= resp.status < 300:
            raise RuntimeError(f"upload failed with HTTP {resp.status}")
        return resp.status


def upload(path: str, presigned_url: Optional[str] = None, presign_endpoint: Optional[str] = None,
           bucket: Optional[str] = None, key: Optional[str] = None, token: Optional[str] = None,
           region: Optional[str] = None, opener=urllib.request.urlopen) -> dict:
    ctype = content_type_for(path)
    name = os.path.basename(path)
    if presigned_url:
        url = presigned_url
    elif presign_endpoint:
        url, key = presign_from_api(presign_endpoint, name, ctype, token=token, opener=opener)
    elif bucket:
        key = key or f"uploads/{name}"
        url = presign_with_boto3(bucket, key, ctype, region=region)
    else:
        raise ValueError("give --presigned-url, --presign-endpoint, or --bucket")
    status = put_file(url, path, ctype, opener=opener)
    return {"file": path, "bytes": os.path.getsize(path), "content_type": ctype, "key": key, "status": status}
