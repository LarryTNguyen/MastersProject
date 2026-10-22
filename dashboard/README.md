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
python -m unittest discover -s tests -t . # 36 tests
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
- **Upload**: drop a file, get a presigned URL from the API, PUT straight to S3.

## API

All responses are JSON. Shapes are in `app/models.py`.

| Route | Returns |
|---|---|
| `GET /api/health` | `{ok, store, traces}` |
| `GET /api/services` | per-service `span_count, trace_count, error_rate, p50_ms, p99_ms` |
| `GET /api/operations?service=` | operation names, for the filter dropdown |
| `GET /api/traces?service&operation&status&min_duration_ms&max_duration_ms&start_after_us&start_before_us&complete&sort&offset&limit` | `{items: [TraceSummary], total, offset, limit}` |
| `GET /api/traces/{trace_id}` | `{summary: TraceSummary, spans: [Span]}` |
| `GET /api/rankings?endpoint&limit` | `[RankingWindow]`, newest window first |
| `POST /api/uploads` `{filename, content_type}` | `{url, key, method, headers, expires_in}` |
| `GET /` | the page |

`sort` is one of `start_desc` (default), `start_asc`, `duration_desc`, `duration_asc`.
`limit` is capped at 500. Bad filter values return 422.

## Where the data comes from

Two stores implement the same interface (`app/store.py`), and the tests run the same
checks against both so they cannot drift apart:

- `JsonlStore`: reads `*.jsonl` files in the common span format, plus an optional
  `rankings.json`. It reloads when files change. Used locally and for demos, and it
  reads the adapter Lambda's output as-is.
- `SqlStore`: reads the tables in `app/schema.sql`. SQLite locally; on Aurora the
  same queries run through psycopg (placeholders are rewritten from `?` to `%s`).

### Tables the dashboard expects (draft, for Larry)

`app/schema.sql` is a proposal. The reconstruction step writes `spans` and `traces`
(one row per trace: root service/operation, start, duration, span count, status,
complete flag) plus `trace_services`, and the diagnosis step writes
`ranking_windows` and `rankings`. If the real tables end up different, only
`app/sql_store.py` needs to change; the page and the API do not care.

`tools/load_sqlite.py` fills those tables from span files the same way, so the
dashboard can be developed and demoed before the cloud tables exist.

### Ranking shape (for Larry)

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
- Environment: `STORE=sqlite` is only for local use. For Aurora, add a store that
  opens the connection (Data API or psycopg) and passes it to `SqlStore(conn, "%s")`;
  that is a few lines in `app/main.py:make_store`.
- Uploads: set `RAW_BUCKET` (and optionally `RAW_PREFIX`, default `uploads/`). The
  Lambda role needs `s3:PutObject` on that prefix, and the bucket needs a CORS rule
  allowing `PUT` from the dashboard's origin, otherwise the browser upload fails.
- Presigned URLs expire after `UPLOAD_EXPIRES` seconds (default 900). Filenames are
  sanitized and prefixed with the date and a random id, so uploads never overwrite
  each other.

## Requirements covered

- **E8** dashboard with trace search, waterfalls, ranked bottlenecks with evidence.
  From a slow episode to a ranked list with supporting traces: Overview → ranking →
  service → slowest traces → waterfall, four clicks.
- **E3** upload path, browser side.
- **D3** the negative result is a first-class thing the page shows, not an empty list.
- **N6** no secrets in the page; the API never returns AWS credentials, only a
  short-lived presigned URL scoped to one key.

Slow-versus-normal comparison, the flame graph view, confidence, and the pipeline
health panel are Sprint 5.
