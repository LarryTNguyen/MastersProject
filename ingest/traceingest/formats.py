"""Detect an input file's format and load it as common spans."""
from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass, field
from typing import IO, List, Optional

from . import alibaba, otlp, rcaeval
from .spans import Span, read_jsonl

ALIBABA = "alibaba"
RCAEVAL = "rcaeval"
OTLP_JSON = "otlp"
SPANS_JSONL = "jsonl"
FORMATS = (ALIBABA, RCAEVAL, OTLP_JSON, SPANS_JSONL)


@dataclass
class LoadResult:
    format: str
    spans: List[Span]
    stats: dict = field(default_factory=dict)


def detect_format(head: str, name: str = "") -> str:
    """Guess the format from the first few KB of a file (and its name)."""
    text = head.lstrip("﻿").lstrip()
    if text.startswith("{"):
        first_line = text.split("\n", 1)[0]
        if '"resourceSpans"' in text[:4096]:
            return OTLP_JSON
        try:
            obj = json.loads(first_line)
            if isinstance(obj, dict) and "trace_id" in obj and "span_id" in obj:
                return SPANS_JSONL
        except ValueError:
            pass
        return OTLP_JSON
    header = next(csv.reader(io.StringIO(text.split("\n", 1)[0])), [])
    if alibaba.looks_like_alibaba(header):
        return ALIBABA
    if rcaeval.looks_like_rcaeval(header):
        return RCAEVAL
    raise ValueError(f"could not recognise the format of {name or 'input'}; header was {header[:10]}")


def load_text(fh: IO[str], fmt: str = "auto", name: str = "", base_epoch_us: int = 0) -> LoadResult:
    """Load spans from an open text stream. ``fmt`` may be 'auto'."""
    if fmt == "auto":
        head = fh.read(8192)
        fmt = detect_format(head, name)
        rest = fh.read()
        fh = io.StringIO(head + rest)
    if fmt == ALIBABA:
        st = alibaba.AlibabaStats()
        spans = alibaba.read_alibaba(fh, base_epoch_us=base_epoch_us, stats=st)
        return LoadResult(fmt, spans, asdict(st))
    if fmt == RCAEVAL:
        st = rcaeval.RCAEvalStats()
        spans = rcaeval.read_rcaeval_csv(fh, stats=st)
        return LoadResult(fmt, spans, asdict(st))
    if fmt == OTLP_JSON:
        text = fh.read()
        spans: List[Span] = []
        stripped = text.strip()
        # one document, or one document per line (what a file exporter writes)
        try:
            docs = [json.loads(stripped)]
        except ValueError:
            docs = [json.loads(line) for line in stripped.splitlines() if line.strip()]
        for d in docs:
            spans.extend(otlp.from_otlp(d))
        spans.sort(key=Span.sort_key)
        return LoadResult(fmt, spans, {"documents": len(docs), "spans": len(spans)})
    if fmt == SPANS_JSONL:
        spans = sorted(read_jsonl(fh), key=Span.sort_key)
        return LoadResult(fmt, spans, {"spans": len(spans)})
    raise ValueError(f"unknown format {fmt!r}; expected one of {FORMATS} or 'auto'")


def load_path(path: str, fmt: str = "auto", base_epoch_us: int = 0) -> LoadResult:
    if path.endswith(".parquet"):
        st = rcaeval.RCAEvalStats()
        return LoadResult(RCAEVAL, rcaeval.read_rcaeval_parquet(path, st), asdict(st))
    if fmt == "auto":
        with open(path, encoding="utf-8", errors="replace") as fh:
            fmt = detect_format(fh.read(8192), path)
    with open(path, newline="", encoding="utf-8") as fh:
        return load_text(fh, fmt=fmt, name=path, base_epoch_us=base_epoch_us)


def first_bytes_text(data: bytes, limit: int = 8192) -> Optional[str]:
    if data[:4] == b"PAR1":
        return None   # parquet
    return data[:limit].decode("utf-8", errors="replace")
