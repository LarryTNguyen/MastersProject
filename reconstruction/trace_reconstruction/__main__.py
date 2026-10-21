"""Command-line entry point for local reconstruction."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .store import PostgresStore
from .topology import group_records
from .worker import ReconstructionWorker


def _validator():
    schema_dir = Path(__file__).resolve().parents[2] / "trace_schema"
    sys.path.insert(0, str(schema_dir))
    from validate import validate_record
    return validate_record


def _records(path: str):
    content = Path(path).read_text(encoding="utf-8")
    try:
        document = json.loads(content)
    except json.JSONDecodeError:
        document = None
    if isinstance(document, list):
        yield from document
        return
    if isinstance(document, dict):
        yield document
        return
    for line_number, line in enumerate(content.splitlines(), start=1):
        if line.strip():
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc


def _store(args) -> PostgresStore:
    return PostgresStore.connect(args.dsn, validator=_validator())


def cmd_inspect(args) -> None:
    groups = group_records(_records(args.input))
    output = []
    for key in sorted(groups):
        trace = groups[key].reconstruct()
        output.append({
            "run_id": trace.run_id,
            "trace_id": trace.trace_id,
            "topology": trace.topology,
            "root_span_id": trace.root_span_id,
            "quality": trace.quality,
        })
    print(json.dumps(output, indent=2))


def cmd_init_run(args) -> None:
    store = _store(args)
    try:
        store.create_run(
            args.run_id,
            application_name=args.application,
            environment=args.environment,
            source_format=args.source_format,
            identity_kind=args.identity_kind,
            dataset_name=args.dataset,
            quiet_window_ms=args.quiet_window_ms,
            max_open_ms=args.max_open_ms,
            configuration={"created_by": "trace-reconstruction-cli"},
        )
    finally:
        store.close()


def cmd_ingest(args) -> None:
    store = _store(args)
    try:
        print(json.dumps(store.ingest_many(_records(args.input)), indent=2))
    finally:
        store.close()


def cmd_poll(args) -> None:
    store = _store(args)
    try:
        worker = ReconstructionWorker(store)
        if args.forever:
            worker.run_forever(interval_seconds=args.interval, limit=args.limit)
        else:
            print(json.dumps(vars(worker.poll_once(limit=args.limit)), indent=2))
    finally:
        store.close()


def cmd_finish(args) -> None:
    store = _store(args)
    try:
        stats = ReconstructionWorker(store).finish_run(args.run_id)
        print(json.dumps(vars(stats), indent=2))
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconstruct canonical distributed traces")
    parser.add_argument(
        "--dsn",
        default="postgresql://trace:trace@localhost:55432/trace",
        help="PostgreSQL DSN",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_parser = sub.add_parser("inspect", help="reconstruct JSONL without a database")
    inspect_parser.add_argument("input")
    inspect_parser.set_defaults(func=cmd_inspect)

    init_parser = sub.add_parser("init-run", help="create an ingestion run")
    init_parser.add_argument("run_id")
    init_parser.add_argument("--application", default="astronomy-shop")
    init_parser.add_argument("--environment", default="local")
    init_parser.add_argument("--source-format", default="otlp_json")
    init_parser.add_argument("--identity-kind", choices=("OTEL", "DATASET"), default="OTEL")
    init_parser.add_argument("--dataset")
    init_parser.add_argument("--quiet-window-ms", type=int, default=5000)
    init_parser.add_argument("--max-open-ms", type=int, default=60000)
    init_parser.set_defaults(func=cmd_init_run)

    ingest_parser = sub.add_parser("ingest", help="ingest canonical JSONL")
    ingest_parser.add_argument("input")
    ingest_parser.set_defaults(func=cmd_ingest)

    poll_parser = sub.add_parser("poll", help="settle due traces and publish topology")
    poll_parser.add_argument("--limit", type=int, default=100)
    poll_parser.add_argument("--interval", type=float, default=1.0)
    poll_parser.add_argument("--forever", action="store_true")
    poll_parser.set_defaults(func=cmd_poll)

    finish_parser = sub.add_parser("finish", help="settle and analyze every open trace in a run")
    finish_parser.add_argument("run_id")
    finish_parser.set_defaults(func=cmd_finish)
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
