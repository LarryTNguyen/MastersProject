"""Command line: python -m traceingest <command> ...

  convert   dataset file (or RCAEval case folder) -> OTLP JSON or canonical JSONL
  replay    play a dataset back to a collector URL or into batch files
  upload    send a file to the raw S3 bucket with a presigned URL
  inspect   print format, counts and data-quality stats for a file
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error

from . import canonical, formats, otlp, rcaeval
from .replay import Replayer
from .sinks import FileSink, HttpSink
from .spans import validate
from .upload import upload


def _load(path: str, fmt: str, base_epoch_us: int):
    """Returns (LoadResult, label or None). A folder is treated as an RCAEval case."""
    if os.path.isdir(path):
        st = rcaeval.RCAEvalStats()
        spans, label = rcaeval.read_case(path, st)
        from dataclasses import asdict
        return formats.LoadResult(formats.RCAEVAL, spans, asdict(st)), label
    return formats.load_path(path, fmt=fmt, base_epoch_us=base_epoch_us), None


def cmd_inspect(a):
    res, label = _load(a.input, a.format, a.base_epoch_us)
    problems = sum(1 for s in res.spans if validate(s))
    services = sorted({s.service for s in res.spans})
    out = {"format": res.format, "spans": len(res.spans), "traces": len({s.trace_id for s in res.spans}),
           "services": len(services), "invalid_spans": problems, "stats": res.stats}
    if label:
        out["label"] = label.to_dict()
    print(json.dumps(out, indent=2, default=str))


def cmd_convert(a):
    res, label = _load(a.input, a.format, a.base_epoch_us)
    spans = res.spans
    os.makedirs(os.path.dirname(os.path.abspath(a.output)) or ".", exist_ok=True)
    if a.to == "jsonl":
        with open(a.output, "w", encoding="utf-8") as fh:
            context = canonical.NormalizationContext.for_source(
                f"file:{os.path.abspath(a.input)}", run_id=a.run_id)
            canonical.write_jsonl(spans, fh, context)
        written = [a.output]
    else:
        written = []
        chunks = list(otlp.split_batches(spans, a.max_spans_per_file)) if a.max_spans_per_file else [spans]
        for i, chunk in enumerate(chunks, start=1):
            path = a.output if len(chunks) == 1 else a.output.replace(".json", f".part{i:03d}.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(otlp.to_otlp(chunk), separators=(",", ":")))
            written.append(path)
    if label:
        label_path = os.path.splitext(a.output)[0] + ".label.json"
        rcaeval.write_label(label, label_path)
        written.append(label_path)
    print(json.dumps({"format": res.format, "spans": len(spans), "written": written, "stats": res.stats},
                     indent=2, default=str))


def cmd_replay(a):
    res, _ = _load(a.input, a.format, a.base_epoch_us)
    if a.url:
        sink = HttpSink(a.url, token=a.token or os.environ.get("STA_INGEST_TOKEN"),
                        header=a.auth_header, scheme=a.auth_scheme)
    else:
        sink = FileSink(a.out_dir, fmt=a.out_format, run_id=a.run_id)
    speed = float("inf") if a.speed == "max" else float(a.speed)

    def progress(batches, spans):
        if not a.quiet:
            print(f"\rsent {spans} spans in {batches} batches", end="", file=sys.stderr, flush=True)

    r = Replayer(sink, speed=speed, max_batch=a.batch_size, flush_interval_s=a.flush_interval,
                 rebase_to_now=a.rebase_to_now, on_batch=progress)
    stats = r.run(res.spans, limit=a.limit)
    if not a.quiet:
        print(file=sys.stderr)
    print(json.dumps(stats.summary(), indent=2))


def cmd_upload(a):
    print(json.dumps(upload(a.file, presigned_url=a.presigned_url, presign_endpoint=a.presign_endpoint,
                            bucket=a.bucket, key=a.key, token=a.token, region=a.region), indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="traceingest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_input(sp):
        sp.add_argument("input", help="dataset file, or an RCAEval case folder")
        sp.add_argument("--format", default="auto", choices=("auto",) + formats.FORMATS)
        sp.add_argument("--base-epoch-us", type=int, default=0,
                        help="anchor for Alibaba's relative timestamps (microseconds since 1970)")
        sp.add_argument("--run-id", help="UUID shared by all normalized records in this replay/experiment")

    sp = sub.add_parser("inspect", help="show format, counts and data-quality stats")
    add_input(sp)
    sp.set_defaults(fn=cmd_inspect)

    sp = sub.add_parser("convert", help="convert a dataset to OTLP JSON or canonical JSONL")
    add_input(sp)
    sp.add_argument("-o", "--output", required=True)
    sp.add_argument("--to", choices=("otlp", "jsonl"), default="otlp")
    sp.add_argument("--max-spans-per-file", type=int, default=0, help="split OTLP output into parts")
    sp.set_defaults(fn=cmd_convert)

    sp = sub.add_parser("replay", help="replay a dataset at 1x, 2x, 10x or max speed")
    add_input(sp)
    sp.add_argument("--speed", default="1", help="1, 2, 10, ... or 'max'")
    sp.add_argument("--url", help="OTLP/HTTP endpoint (collector Function URL or http://localhost:4318)")
    sp.add_argument("--token", help="ingest token (or set STA_INGEST_TOKEN)")
    sp.add_argument("--auth-header", default="Authorization")
    sp.add_argument("--auth-scheme", default="Bearer", help="use '' to send the raw token")
    sp.add_argument("--out-dir", default="replay_out", help="where batch files go when no --url")
    sp.add_argument("--out-format", choices=("otlp", "jsonl"), default="otlp")
    sp.add_argument("--batch-size", type=int, default=512)
    sp.add_argument("--flush-interval", type=float, default=1.0)
    sp.add_argument("--limit", type=int, help="only replay the first N spans")
    sp.add_argument("--rebase-to-now", action="store_true", help="shift timestamps to the current time")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(fn=cmd_replay)

    sp = sub.add_parser("upload", help="upload a trace file to the raw bucket via a presigned URL")
    sp.add_argument("file")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--presigned-url")
    g.add_argument("--presign-endpoint", help="dashboard API endpoint that returns a presigned URL")
    g.add_argument("--bucket", help="sign the URL yourself with your AWS credentials")
    sp.add_argument("--key", help="object key when using --bucket (default uploads/<filename>)")
    sp.add_argument("--token")
    sp.add_argument("--region")
    sp.set_defaults(fn=cmd_upload)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except urllib.error.HTTPError as e:
        hint = " (check the token)" if e.code in (401, 403) else ""
        sys.exit(f"error: the server answered HTTP {e.code}{hint}")
    except (ValueError, FileNotFoundError, RuntimeError, urllib.error.URLError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
