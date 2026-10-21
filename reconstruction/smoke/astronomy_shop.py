"""Prepare and verify the local Astronomy Shop payment-failure smoke test.

The controller never edits the upstream demo checkout.  It writes a private
flagd configuration and Compose override into a selected runtime directory.
The same runtime can be switched between healthy and faulted configurations,
and a manifest records the exact injection window and shared trace run ID.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _set_payment_variant(configuration: dict, variant: str) -> None:
    payment = configuration.get("flags", {}).get("paymentFailure")
    if payment is None:
        raise ValueError("source configuration does not contain paymentFailure")
    if variant not in payment.get("variants", {}):
        raise ValueError(f"paymentFailure has no {variant!r} variant")
    payment["defaultVariant"] = variant


def cmd_prepare(args) -> None:
    runtime = Path(args.runtime).resolve()
    source = Path(args.demo_dir).resolve() / "src" / "flagd" / "demo.flagd.json"
    flags = _load(source)
    _set_payment_variant(flags, "off")
    flag_path = runtime / "demo.flagd.json"
    _write_json(flag_path, flags)

    override = runtime / "astronomy-smoke.override.yaml"
    override.write_text(
        "services:\n"
        "  flagd:\n"
        "    volumes:\n"
        "      - ${STA_FLAG_CONFIG}:/etc/flagd/demo.flagd.json:ro\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "run_id": args.run_id or str(uuid.uuid4()),
        "demo_directory": str(Path(args.demo_dir).resolve()),
        "demo_version": args.demo_version,
        "fault": "paymentFailure",
        "fault_variant": "100%",
        "root_cause_service": "payment",
        "minimum_healthy_checkout_traces": args.minimum_healthy,
        "minimum_faulty_checkout_traces": args.minimum_faulty,
        "healthy_started_at_unix_ns": str(time.time_ns()),
        "injected_at_unix_ns": None,
        "cleared_at_unix_ns": None,
    }
    _write_json(runtime / "manifest.json", manifest)
    print(json.dumps({
        "run_id": manifest["run_id"],
        "manifest": str(runtime / "manifest.json"),
        "flag_config": str(flag_path),
        "compose_override": str(override),
    }, indent=2))


def _change(runtime_value: str, variant: str, timestamp_field: str) -> None:
    runtime = Path(runtime_value).resolve()
    flag_path = runtime / "demo.flagd.json"
    manifest_path = runtime / "manifest.json"
    flags = _load(flag_path)
    manifest = _load(manifest_path)
    _set_payment_variant(flags, variant)
    _write_json(flag_path, flags)
    manifest[timestamp_field] = str(time.time_ns())
    _write_json(manifest_path, manifest)
    print(json.dumps({"paymentFailure": variant, timestamp_field: manifest[timestamp_field]}, indent=2))


def cmd_inject(args) -> None:
    _change(args.runtime, "100%", "injected_at_unix_ns")


def cmd_clear(args) -> None:
    _change(args.runtime, "off", "cleared_at_unix_ns")


def _read_jsonl(path: Path) -> list[dict]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return records


def cmd_verify(args) -> None:
    runtime = Path(args.runtime).resolve()
    manifest = _load(runtime / "manifest.json")
    injected = manifest.get("injected_at_unix_ns")
    if injected is None:
        raise ValueError("manifest has no injection timestamp; run inject first")
    injected_ns = int(injected)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from trace_reconstruction.topology import group_records

    records = _read_jsonl(Path(args.input))
    wrong_run = sorted({record.get("run_id") for record in records if record.get("run_id") != manifest["run_id"]})
    groups = group_records(records)
    healthy = 0
    faulty = 0
    invalid = []
    missing_payment_error = []
    for (_, trace_id), group in groups.items():
        trace = group.reconstruct()
        if trace.topology == "INVALID":
            invalid.append(trace_id)
        roots = [span for span in trace.spans if span.get("parent_relation") == "ROOT"]
        if len(roots) != 1 or roots[0].get("start_time_ns") is None:
            continue
        root = roots[0]
        is_checkout = "checkout" in root.get("service_name", "").lower() or "checkout" in (root.get("operation_name") or "").lower()
        if not is_checkout:
            continue
        if int(root["start_time_ns"]) < injected_ns:
            healthy += 1
            continue
        faulty += 1
        payment_errors = [
            span for span in trace.spans
            if span.get("service_name") == "payment" and span.get("status", {}).get("code") == "ERROR"
        ]
        if not payment_errors:
            missing_payment_error.append(trace_id)

    failures = []
    if wrong_run:
        failures.append(f"records contain unexpected run IDs: {wrong_run}")
    if healthy < manifest["minimum_healthy_checkout_traces"]:
        failures.append(f"only {healthy} healthy checkout traces")
    if faulty < manifest["minimum_faulty_checkout_traces"]:
        failures.append(f"only {faulty} faulty-window checkout traces")
    if missing_payment_error:
        failures.append(f"fault-window checkout traces without payment ERROR: {missing_payment_error[:10]}")
    if invalid:
        failures.append(f"invalid trace topology: {invalid[:10]}")

    result = {
        "passed": not failures,
        "run_id": manifest["run_id"],
        "healthy_checkout_traces": healthy,
        "faulty_checkout_traces": faulty,
        "invalid_trace_count": len(invalid),
        "faulty_without_payment_error_count": len(missing_payment_error),
        "failures": failures,
    }
    print(json.dumps(result, indent=2))
    if failures:
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Astronomy Shop payment-failure smoke test")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--demo-dir", required=True)
    prepare.add_argument("--runtime", default=".astronomy-smoke")
    prepare.add_argument("--demo-version", required=True, help="pinned tag or commit")
    prepare.add_argument("--run-id")
    prepare.add_argument("--minimum-healthy", type=int, default=5)
    prepare.add_argument("--minimum-faulty", type=int, default=5)
    prepare.set_defaults(func=cmd_prepare)

    for name, function in (("inject", cmd_inject), ("clear", cmd_clear)):
        command = sub.add_parser(name)
        command.add_argument("--runtime", default=".astronomy-smoke")
        command.set_defaults(func=function)

    verify = sub.add_parser("verify")
    verify.add_argument("--runtime", default=".astronomy-smoke")
    verify.add_argument("--input", required=True, help="canonical JSONL captured during the run")
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
