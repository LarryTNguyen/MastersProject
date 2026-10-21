"""Where replayed span batches go: files, an OTLP/HTTP endpoint, or memory."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import List, Optional

from . import otlp
from .canonical import NormalizationContext, write_jsonl as write_canonical_jsonl
from .spans import Span


class Sink:
    def send(self, batch: List[Span]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class MemorySink(Sink):
    """Keeps every batch; used by tests and dry runs."""

    def __init__(self, clock=time.monotonic):
        self.batches: List[List[Span]] = []
        self.sent_at: List[float] = []
        self._clock = clock

    def send(self, batch):
        self.batches.append(list(batch))
        self.sent_at.append(self._clock())

    @property
    def spans(self) -> List[Span]:
        return [s for b in self.batches for s in b]


class FileSink(Sink):
    """Writes each batch as its own file: OTLP JSON (upload-ready) or canonical JSONL."""

    def __init__(self, out_dir: str, fmt: str = "otlp", prefix: str = "batch", run_id: Optional[str] = None):
        if fmt not in ("otlp", "jsonl"):
            raise ValueError("fmt must be 'otlp' or 'jsonl'")
        os.makedirs(out_dir, exist_ok=True)
        self.out_dir, self.fmt, self.prefix, self.count = out_dir, fmt, prefix, 0
        self.context = NormalizationContext.for_source(f"replay:{os.path.abspath(out_dir)}", run_id=run_id)

    def send(self, batch):
        self.count += 1
        ext = "json" if self.fmt == "otlp" else "jsonl"
        path = os.path.join(self.out_dir, f"{self.prefix}_{self.count:06d}.{ext}")
        with open(path, "w", encoding="utf-8") as fh:
            if self.fmt == "otlp":
                fh.write(json.dumps(otlp.to_otlp(batch), separators=(",", ":")))
            else:
                write_canonical_jsonl(batch, fh, self.context)


class HttpSink(Sink):
    """POSTs OTLP/JSON to a collector (the collector Lambda's Function URL, or any
    OTLP/HTTP endpoint such as a local OpenTelemetry Collector on :4318)."""

    def __init__(self, url: str, token: Optional[str] = None, header: str = "Authorization",
                 scheme: str = "Bearer", timeout: float = 10.0, retries: int = 3,
                 opener=urllib.request.urlopen, sleep=time.sleep):
        if url.rstrip("/").endswith(":4318"):   # bare OTel Collector address
            url = url.rstrip("/") + "/v1/traces"
        self.url, self.token, self.header, self.scheme = url, token, header, scheme
        self.timeout, self.retries, self._open, self._sleep = timeout, retries, opener, sleep
        self.sent = 0
        self.failed = 0

    def _request(self, body: bytes) -> urllib.request.Request:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers[self.header] = f"{self.scheme} {self.token}".strip() if self.scheme else self.token
        return urllib.request.Request(self.url, data=body, headers=headers, method="POST")

    def send(self, batch):
        body = json.dumps(otlp.to_otlp(batch), separators=(",", ":")).encode("utf-8")
        delay = 0.5
        for attempt in range(self.retries + 1):
            try:
                with self._open(self._request(body), timeout=self.timeout) as resp:
                    if 200 <= resp.status < 300:
                        self.sent += len(batch)
                        return
                    raise urllib.error.HTTPError(self.url, resp.status, "bad status", resp.headers, None)
            except urllib.error.HTTPError as e:
                # 4xx (except 429) will not get better on retry, e.g. a wrong token
                if 400 <= e.code < 500 and e.code != 429:
                    self.failed += len(batch)
                    raise
                err = e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                err = e
            if attempt < self.retries:
                self._sleep(delay)
                delay *= 2
        self.failed += len(batch)
        raise RuntimeError(f"sending {len(batch)} spans to {self.url} failed after retries: {err}")
