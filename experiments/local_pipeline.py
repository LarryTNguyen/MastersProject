"""Run the whole pipeline on one machine and measure freshness (requirement N1).

    docker compose up -d postgres                       # from the repo root
    python -m experiments.local_pipeline data/rcaeval/re2ob_checkoutservice_cpu_1 --seconds 120

What runs (everything after the collector is the same code the AWS Lambdas run):

  replayer   python -m traceingest replay <input> --speed 1 --rebase-to-now --url <collector>
  collector  small local HTTP endpoint: checks the token, stores each batch as raw/<id>.json and
             notes when it arrived (stand-in for the collector Lambda, which does the same)
  adapter    traceingest.adapter_lambda.handler, fed S3-style events, writes normalized/*.jsonl
  database   trace_reconstruction.PostgresStore.ingest_many + ReconstructionWorker.poll_once
  dashboard  app.tracev1_store.TraceV1Store.pipeline(): the numbers the Pipeline view shows

Results go to <out>/freshness.json and <out>/summary.md. experiments.run picks the json
up with "freshness": {"file": ".../freshness.json"}. On AWS the queue and Lambda start-up
add delay, so treat the local numbers as a lower bound.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

from . import _paths

_paths.setup()
ROOT = _paths.ROOT
for sub in ("reconstruction", "trace_schema"):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from traceingest import adapter_lambda  # noqa: E402

DEFAULT_DSN = "postgresql://trace:trace@localhost:55432/trace"


# ---------------------------------------------------------------- local "S3"
class FolderS3:
    """The two S3 calls the adapter makes, backed by a folder (bucket name is ignored)."""

    def __init__(self, base: str):
        self.base = base

    def _path(self, key: str) -> str:
        return os.path.join(self.base, *key.split("/"))

    def get_object(self, Bucket: str, Key: str) -> dict:
        with open(self._path(Key), "rb") as f:
            return {"Body": io.BytesIO(f.read())}

    def put_object(self, Bucket: str, Key: str, Body: bytes, **_) -> dict:
        path = self._path(Key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(Body)
        return {}


# ---------------------------------------------------------------- collector
def start_collector(base: str, token: str, arrivals: "queue.Queue[tuple]"):
    """POST /v1/traces with Authorization: Bearer <token> -> raw/<id>.json. Returns (server, url)."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path.rstrip("/") != "/v1/traces":
                self.send_response(404); self.end_headers(); return
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_response(401); self.end_headers(); return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received = datetime.now(timezone.utc)
            key = f"raw/{received:%Y/%m/%d}/{uuid.uuid4().hex}.json"
            FolderS3(base).put_object(Bucket="local", Key=key, Body=body)
            arrivals.put((key, received))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):      # keep the console quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1/traces"


def s3_event(key: str, received: datetime) -> dict:
    body = {"Records": [{"eventTime": received.isoformat().replace("+00:00", "Z"),
                         "s3": {"bucket": {"name": "local"}, "object": {"key": key}}}]}
    return {"Records": [{"messageId": key, "body": json.dumps(body)}]}


def read_jsonl(path: str) -> List[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------- measurement
SPAN_END_NS = ("COALESCE(s.end_time_ns, (s.data->'source'->'details'->>'replay_start_ns')::bigint"
               " + (s.data->>'duration_ns')::bigint)")

def settle_latency_ms(conn, run_id: str) -> List[float]:
    """Per trace: when it settled minus when its last span ended (both wall clock, ms).
    Alibaba call observations have no absolute end time, so their replay start plus
    duration is used instead (the same fallback the dashboard uses)."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT EXTRACT(EPOCH FROM t.settled_at) * 1000 - max({SPAN_END_NS}) / 1e6 "
            "FROM trace_v1.traces t JOIN trace_v1.spans s ON s.run_id = t.run_id AND s.trace_id = t.trace_id "
            f"WHERE t.run_id = %s AND t.settled_at IS NOT NULL AND {SPAN_END_NS} IS NOT NULL "
            "GROUP BY t.trace_id, t.settled_at", (run_id,))
        return [float(r[0]) for r in cur.fetchall()]


def pct(values: List[float], p: float) -> Optional[float]:
    """Nearest-rank percentile, the same rule the dashboard uses."""
    from app.stitch import percentile
    v = percentile(values, p / 100)
    return None if v is None else round(v, 1)


def run(args) -> dict:
    import psycopg
    from trace_reconstruction.store import PostgresStore
    from trace_reconstruction.worker import ReconstructionWorker
    from validate import validate_record
    from app.tracev1_store import TraceV1Store

    out = os.path.abspath(args.out)
    work = os.path.join(out, "bucket")
    os.makedirs(work, exist_ok=True)
    run_id = str(uuid.uuid4())
    os.environ["TRACE_RUN_ID"] = run_id
    os.environ["NORMALIZED_PREFIX"] = "normalized/"
    s3 = FolderS3(work)
    token = secrets.token_hex(16)
    arrivals: "queue.Queue[tuple]" = queue.Queue()
    server, url = start_collector(work, token, arrivals)

    store = PostgresStore.connect(args.dsn, validator=validate_record)
    worker = ReconstructionWorker(store)
    run_created = False
    counts = {"batches": 0, "spans": 0, "adapter_failures": 0}
    ingest = {}
    busy = {"adapter": 0.0, "ingest": 0.0, "worker": 0.0}     # seconds spent in each stage

    def poll_worker():
        t = time.perf_counter()
        worker.poll_once(limit=1000)
        busy["worker"] += time.perf_counter() - t

    cmd = [sys.executable, "-m", "traceingest", "replay", os.path.abspath(args.input), "--speed", str(args.speed),
           "--url", url, "--token", token, "--rebase-to-now", "--quiet"]
    print(f"run {run_id}: replaying {args.input} at {args.speed}x for {args.seconds}s", file=sys.stderr)
    replayer = subprocess.Popen(cmd, cwd=os.path.join(ROOT, "ingest"), stdout=subprocess.DEVNULL)
    started = time.time()
    last_arrival = time.time()

    def drain_once() -> int:
        nonlocal run_created, last_arrival
        handled = 0
        while True:
            try:
                key, received = arrivals.get_nowait()
            except queue.Empty:
                return handled
            last_arrival = time.time()
            t = time.perf_counter()
            with contextlib.redirect_stdout(io.StringIO()):     # the handler prints one log line per file
                result = adapter_lambda.handler(s3_event(key, received), s3=s3)
            busy["adapter"] += time.perf_counter() - t
            counts["adapter_failures"] += len(result["batchItemFailures"])
            for r in result["results"]:
                records = read_jsonl(os.path.join(work, *r["output"].split("/", 3)[3].split("/")))
                if not records:
                    continue
                if not run_created:
                    store.create_run(run_id, application_name="local-pipeline", environment="local",
                                     source_format=r["format"], identity_kind=records[0]["identity_kind"],
                                     dataset_name=os.path.basename(args.input.rstrip("/")))
                    run_created = True
                t = time.perf_counter()
                for k, v in store.ingest_many(records).items():
                    ingest[k] = ingest.get(k, 0) + v
                busy["ingest"] += time.perf_counter() - t
                counts["spans"] += len(records)
            counts["batches"] += 1
            handled += 1

    try:
        while time.time() - started < args.seconds and replayer.poll() is None:
            drain_once()
            poll_worker()
            time.sleep(0.2)
    finally:
        if replayer.poll() is None:
            replayer.terminate()
            replayer.wait()
    # let in-flight batches land, then give the quiet window time to settle the last traces
    while time.time() - last_arrival < 2:
        drain_once()
        time.sleep(0.2)
    deadline = time.time() + args.settle_wait
    while time.time() < deadline:
        drain_once()
        poll_worker()
        time.sleep(0.5)
    server.shutdown()
    elapsed = time.time() - started

    conn = psycopg.connect(args.dsn, autocommit=True)
    dash = TraceV1Store(conn, run_id=run_id)
    pipe = dash.pipeline(now_us=int(time.time() * 1_000_000), window_s=max(900, int(args.seconds) + 600))
    settle = settle_latency_ms(conn, run_id)
    with conn.cursor() as cur:
        cur.execute("SELECT state, count(*) FROM trace_v1.traces WHERE run_id = %s GROUP BY state", (run_id,))
        states = dict(cur.fetchall())
    result = {
        "run_id": run_id, "input": args.input, "speed": args.speed, "seconds": args.seconds,
        "where": "one machine, local PostgreSQL 16", "target_p95_ms": 60000,
        "staleness_ms": pipe["staleness_ms"], "lag_ms": pipe["lag_ms"],
        "settle_ms": {"p50": pct(settle, 50), "p95": pct(settle, 95), "p99": pct(settle, 99), "traces": len(settle)},
        "spans_with_stamps": pipe["spans_with_stamps"], "spans": counts["spans"], "batches": counts["batches"],
        "ingest": ingest,
        "adapter_failures": counts["adapter_failures"], "trace_states": states,
        "incomplete_rate": pipe["incomplete_rate"],
        "elapsed_s": round(elapsed, 1),
        "busy_s": {k: round(v, 1) for k, v in busy.items()},
        "staleness_ms_values": [],
    }
    if args.keep_values:
        with conn.cursor() as cur:
            cur.execute(f"SELECT (s.data->'attributes'->'sta.normalized_us'->>'value')::bigint / 1000.0 - {SPAN_END_NS} / 1e6 "
                        f"FROM trace_v1.spans s WHERE s.run_id = %s AND {SPAN_END_NS} IS NOT NULL", (run_id,))
            result["staleness_ms_values"] = [round(float(r[0]), 1) for r in cur.fetchall() if r[0] is not None]
    conn.close()
    store.close()
    with open(os.path.join(out, "freshness.json"), "w") as f:
        json.dump(result, f, indent=2)
    with open(os.path.join(out, "summary.md"), "w") as f:
        f.write(summary_md(result))
    return result


def summary_md(r: dict) -> str:
    s, st = r["staleness_ms"], r["settle_ms"]
    busy = ", ".join(f"{k} {v:.0f} s" for k, v in r.get("busy_s", {}).items()) + f" of {r.get('elapsed_s', 0):.0f} s"
    ok = s.get("p95") is not None and s["p95"] < r["target_p95_ms"]
    fmt = lambda v: "–" if v is None else f"{v / 1000:.2f} s"
    return (
        f"# Local freshness run\n\n"
        f"- input: `{r['input']}` at {r['speed']}x for {r['seconds']:.0f} s ({r['where']})\n"
        f"- spans: {r['spans']} in {r['batches']} batches; ingest {r['ingest']}; adapter failures {r['adapter_failures']}\n"
        f"- trace states at the end: {r['trace_states']}\n\n"
        f"| | p50 | p95 | p99 |\n|---|---|---|---|\n"
        f"| staleness (span end → normalized, what the Pipeline view shows) | {fmt(s.get('p50'))} | {fmt(s.get('p95'))} | {fmt(s.get('p99'))} |\n"
        f"| adapter lag (collector → normalized) | {fmt(r['lag_ms'].get('p50'))} | {fmt(r['lag_ms'].get('p95'))} | – |\n"
        f"| settle (last span end → trace settled) | {fmt(st['p50'])} | {fmt(st['p95'])} | {fmt(st['p99'])} |\n\n"
        f"Time spent per stage (one process): {busy}.\n\n"
        f"N1 target: p95 staleness under 60 s → **{'met' if ok else 'not met'}** locally. "
        f"Settle time includes the 5 s quiet window by design. AWS adds the SQS hop and Lambda start-up, "
        f"so these are a lower bound for the deployed pipeline.\n")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="dataset file or RCAEval case folder")
    p.add_argument("--seconds", type=float, default=120, help="how long to replay (default 120)")
    p.add_argument("--speed", default="1", help="replay speed: 1, 2, 10 or max (default 1, which is what N1 is about)")
    p.add_argument("--settle-wait", type=float, default=12, help="seconds to keep polling after the replay so the last traces settle")
    p.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN))
    p.add_argument("--out", default=os.path.join(ROOT, "experiments", "results", time.strftime("local-freshness-%Y%m%d-%H%M%S")))
    p.add_argument("--keep-values", action="store_true", help="also store every staleness value (for a CDF figure)")
    a = p.parse_args(argv)
    r = run(a)
    print(summary_md(r))


if __name__ == "__main__":
    main()
