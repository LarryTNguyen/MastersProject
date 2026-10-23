# dashboard: API and web page

Owner: Anukrithi (Sprint 4)

The dashboard is one FastAPI app that serves both the JSON API and the page. It runs
locally with uvicorn and in AWS as a Lambda behind a Function URL (Mangum). There is
nothing else to host: the page is a single `static/index.html`.

## Run it locally

```bash
cd dashboard
pip install -r requirements.txt
python tools/make_demo_data.py            # 200 demo traces + an example ranking in data/
uvicorn app.main:app --reload             # http://127.0.0.1:8000
python -m unittest discover -s tests -t . # the tests (see Tests below)
```

With real data instead of the demo set, point `DATA_DIR` at a folder of normalized
span files (the adapter's output):

```bash
python -m traceingest convert ../some/alibaba_hour.csv -o data/alibaba.jsonl --to jsonl   # from ingest/
DATA_DIR=data uvicorn app.main:app --reload
```

Or load the same files into SQLite, which is the same code path Aurora will use:

```bash
python tools/load_sqlite.py data traces.db
STORE=sqlite SQLITE_PATH=traces.db uvicorn app.main:app --reload
```

## What the page does

- **Overview**: every service with span/trace counts, error rate, p50 and p99; the
  slowest traces; the latest bottleneck ranking.
- **Traces**: search by service, endpoint text, status, min/max duration, complete or
  not, sorted by time or duration, with paging. Clicking a trace opens it.
- **Trace detail**: summary, then a waterfall of all spans as a tree (indented by
  depth, colored by service, one bar per span placed by start offset and duration).
  The thin black line under a bar is the span's self time (duration minus its
  children). Hover for the span's fields and attributes. Spans with no recorded
  duration are striped. A span table sits under the waterfall.
- **Bottlenecks**: one block per analysis window with rank, score, and the evidence
  the diagnosis step recorded. A window can carry a note instead of a list (the "no
  service clearly explains the slowdown" case). Click a service to jump to its
  slowest traces.
- **Bottlenecks → Slow versus normal**: for one endpoint, the slowest 10% of
  traces (or everything over a threshold) against the rest: each service's self
  time in both groups, how much it adds, and its share of the critical path. This
  is the evidence behind a ranking, computed from the spans themselves.
- **Flame graph**: many traces merged into one tree, width = self time summed over
  the traces, for all traces or only the slow ones. Click to zoom.
- **Pipeline**: freshness (staleness p50/p95/p99 against the 60 s target, adapter
  lag), incomplete-trace rate, spans normalized per minute. Needs the ingest stamps
  the adapter adds (`sta.received_us`, `sta.normalized_us`).
- **Upload**: drop a file, get a presigned URL from the API, PUT straight to S3.
- A trace's URL (`#trace=<id>`) can be shared; it opens straight to that trace.

## API

All responses are JSON. Shapes are in `app/models.py`.

| Route | Returns |
|---|---|
| `GET /api/health` | `{ok, store, traces}` |
| `GET /api/services` | per-service `span_count, trace_count, error_rate, p50_ms, p99_ms` |
| `GET /api/operations?service=` | operation names, for the filter dropdown |
| `GET /api/traces?service&operation&status&min_duration_ms&max_duration_ms&start_after_us&start_before_us&complete&sort&offset&limit` | `{items: [TraceSummary], total, offset, limit}` |
| `GET /api/traces/{trace_id}` | `{summary: TraceSummary, spans: [Span]}` |
| `GET /api/rankings?endpoint&limit` | `[RankingWindow]`, newest window first; a window may carry `confidence` |
| `GET /api/compare?endpoint&threshold_ms&limit` | slow-versus-normal self time per service |
| `GET /api/flame?endpoint&subset&limit` | merged tree: `{name, service, value, total, count, children}` |
| `GET /api/pipeline?window_s` | staleness/lag percentiles, incomplete rate, ingest rate, per-minute series |
| `POST /api/uploads` `{filename, content_type}` | `{url, key, method, headers, expires_in}` |
| `GET /` | the page |

`sort` is one of `start_desc` (default), `start_asc`, `duration_desc`, `duration_asc`.
`limit` is capped at 500. Bad filter values return 422.

## Where the data comes from

Three stores implement the same interface (`app/store.py`), and the same contract
tests run against each so they cannot drift apart:

- `TraceV1Store` (`app/tracev1_store.py`): **the real one.** Reads the reconstruction
  step's PostgreSQL tables (`trace_v1.spans`, `traces`, `trace_inventory`,
  `current_trace_analyses`, `service_window_results`, see `trace_schema/schema.sql`).
  One ingestion run is shown at a time (`RUN_ID`, default the newest; `/api/runs`
  lists them). Rankings come from `service_window_results`; `confidence`
  HIGH/MEDIUM/LOW/UNAVAILABLE is shown as-is and also mapped to 0.9/0.6/0.3/none,
  and an UNAVAILABLE window is shown as the "no clear bottleneck" result.
  ```bash
  docker compose up -d postgres                         # from the repo root
  STORE=trace_v1 DATABASE_URL=postgresql://trace:trace@localhost:55432/trace uvicorn app.main:app
  ```
- `JsonlStore`: reads normalized `*.jsonl` files straight from the adapter (trace
  schema v1 records; the older flat format is still accepted for the demo data),
  plus an optional `rankings.json`. Reloads when files change. For local work and demos.
- `SqlStore`: a small SQLite layout (`app/schema.sql`) for local development without
  Docker. `tools/load_sqlite.py` fills it from span files.

`app/canonical.py` turns a trace-schema v1 record into the flat span the page uses:
Alibaba call observations get their start from the replay timestamp in the
record's provenance, `UNKNOWN` status becomes `unset`, typed attribute values are
unwrapped, and `received_at_unix_ns` becomes `sta.received_us` for the freshness view.

### Ranking shape (JSONL store and the diagnosis step's table)

For `TraceV1Store` the diagnosis step writes `service_window_results` rows and the
dashboard groups them by window and endpoint (`endpoint_key = '*'` means all
endpoints). `signals` (object) and `evidence` (array of `{name, value}`) are
flattened into the evidence line on the page. For the JSONL store the same thing
is a `rankings.json`:

```json
{"window_start_us": 1760428800000000, "window_end_us": 1760429100000000,
 "endpoint": "HTTP POST /cart/checkout", "method": "bottleneck_score", "note": null,
 "rankings": [{"rank": 1, "service": "paymentservice", "score": 0.91,
               "evidence": {"critical_path_ms_slow": 186.0, "critical_path_ms_normal": 12.1,
                            "self_time_ratio": 0.78, "error_rate": 0.08}}]}
```

`evidence` is free-form; whatever keys the diagnosis records are shown on the page
as-is. A window with an empty `rankings` list and a `note` is the honest
"no clear bottleneck" answer.

## Deploying (for Matthew)

- Package `app/` and `static/` with `fastapi`, `mangum`, `pydantic` into the Lambda
  (or a layer). Handler: `app.lambda_handler.handler`. 512 MB is plenty.
- Function URL with auth type NONE for now (the page is public read-only); we can
  switch to IAM or add a token later.
- Environment: `STORE=trace_v1` with `DATABASE_URL` (from Secrets Manager) and
  optionally `RUN_ID`. `psycopg[binary]` must be in the package. `STORE=sqlite` and
  `STORE=jsonl` are for local use only.
- Logging: standard `logging` to stderr (CloudWatch in Lambda). On the first request
  one line says which store and run is in use (the database password is never
  printed); every request is logged with method, path, status and elapsed ms, at
  INFO when it took over `SLOW_REQUEST_MS` (default 250) and at DEBUG otherwise.
  `LOG_LEVEL` sets the level (default INFO).
- Uploads: set `RAW_BUCKET` (and optionally `RAW_PREFIX`, default `uploads/`). The
  Lambda role needs `s3:PutObject` on that prefix, and the bucket needs a CORS rule
  allowing `PUT` from the dashboard's origin, otherwise the browser upload fails.
- Presigned URLs expire after `UPLOAD_EXPIRES` seconds (default 900). Filenames are
  sanitized and prefixed with the date and a random id, so uploads never overwrite
  each other.

## Analysis in `app/analysis.py`

Self time (duration minus the union of the children's intervals), the critical path
(walk back from a span's end through the children that finished before it, so
sequential children all count and overlapping ones only the last), the
slow-versus-normal split, flame-tree merging, and the pipeline stats. The baselines
in `experiments/` reuse the same functions. The diagnosis step has its own fuller
versions; these exist so the page can show evidence from raw spans on its own.

## Performance

The JSONL store indexes everything once per reload (files are re-checked at most
every 2 s). With 220k spans: `/api/services` 4 ms, trace search 20–30 ms, first load
about 3 s and 2.5 KB of memory per span, so it is fine up to a few hundred thousand
spans; beyond that use the SQL store. The SQL store answers `/api/services` with two
queries regardless of the number of services. The waterfall draws at most 600 rows
(the span table has all of them).

## Tests

```bash
python -m unittest discover -s tests -t .      # 67 unit tests, no database or browser needed
TEST_DATABASE_URL=postgresql://trace:trace@localhost:55432/trace python -m unittest tests.test_tracev1_store   # 6 contract tests on the real schema
pip install playwright && playwright install chromium
python -m unittest tests.test_ui               # 3 browser smoke tests on the demo data
```

## Requirements covered

- **E8** dashboard with trace search, waterfalls, ranked bottlenecks with evidence.
  From a slow episode to a ranked list with supporting traces: Overview → ranking →
  service → slowest traces → waterfall, four clicks.
- **E3** upload path, browser side.
- **D3** the negative result is a first-class thing the page shows, not an empty list.
- **N6** no secrets in the page; the API never returns AWS credentials, only a
  short-lived presigned URL scoped to one key.

- **N1** freshness is measured and shown, not assumed: the Pipeline view reports
  staleness percentiles from the ingest stamps.
- **D1** flame graph; **D4** pipeline health panel.
- **E9/E10** the `experiments/` runner uses this API for the freshness numbers.
