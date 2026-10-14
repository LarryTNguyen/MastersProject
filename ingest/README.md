# ingest: dataset adapter, replayer, and upload path

Owner: Anukrithi (Sprint 3)

This folder covers the "data in" side of the pipeline:

- **Normalize** Alibaba call-graph CSVs, RCAEval traces, and OTLP JSON into one common span format.
- **Replay** a dataset as if it were live traffic, at 1x, 2x, 10x or max speed (E2).
- **Upload** a trace file straight to the raw S3 bucket with a presigned URL (E3).
- **Dataset Adapter Lambda**: SQS (S3 events) → read raw object → normalize → write JSONL to `normalized/`.

Plain Python 3.10+, standard library only. boto3 is only needed for self-signed uploads (and it is already in the Lambda runtime). Reading RCAEval's Parquet copy needs pandas + pyarrow.

## Quick start

```bash
cd ingest
python -m unittest discover -s tests -t .        # 45 tests, one file per module

python -m traceingest inspect tests/fixtures/alibaba_small.csv
python -m traceingest convert tests/fixtures/alibaba_small.csv -o out/alibaba.json          # OTLP JSON
python -m traceingest convert tests/fixtures/rcaeval_small.csv -o out/rcaeval.jsonl --to jsonl
python -m traceingest convert path/to/re2ob_checkoutservice_delay_1 -o out/case.json       # + case.label.json

# replay to a local OpenTelemetry Collector, or to the collector Lambda's Function URL
python -m traceingest replay data/alibaba_hour.csv --speed 10 --url http://localhost:4318 --rebase-to-now
python -m traceingest replay data/alibaba_hour.csv --speed max --url "$COLLECTOR_URL" --token "$STA_INGEST_TOKEN"
python -m traceingest replay data/alibaba_hour.csv --speed max --out-dir replay_out        # batch files, no network

# upload (any one of the three)
python -m traceingest upload out/alibaba.json --presigned-url "https://...amazonaws.com/...&X-Amz-Signature=..."
python -m traceingest upload out/alibaba.json --bucket sta-raw-dev --key uploads/alibaba.json   # uses your AWS creds
python -m traceingest upload out/alibaba.json --presign-endpoint "$DASHBOARD_URL/uploads"
```

## Common span format (draft, Larry please confirm)

One JSON object per line (`.jsonl`). This is the contract between the adapter and trace reconstruction.

| field | type | notes |
|---|---|---|
| `trace_id` | string | original ID from the source |
| `span_id` | string | Alibaba: hash of (trace, rpcid), since rows have no span ID |
| `parent_span_id` | string or null | null for a root span |
| `service` | string | the service doing the work (Alibaba: the downstream `dm`) |
| `operation` | string | endpoint / method name |
| `start_us` | int | microseconds; epoch for RCAEval/OTLP, relative for Alibaba unless `--base-epoch-us` is set |
| `duration_us` | int or null | null when the source did not record it (Alibaba negative `rt`) |
| `status` | `"unset"` / `"ok"` / `"error"` | HTTP ≥ 500 or gRPC ≠ 0 → error |
| `kind` | string or null | server / client / internal / producer / consumer |
| `peer_service` | string or null | caller service (Alibaba `um`) |
| `source` | string | `alibaba` / `rcaeval` / `otlp` |
| `attributes` | object | source extras, e.g. `alibaba.rpcid`, `rcaeval.status_code`, `http.method` |
| `v` | int | schema version (1) |

## How each source is handled

**Alibaba (v2021 / v2022).** One row = one call from `um` to `dm`, and `rpcid` gives the tree (`0.1.3` is a child of `0.1`).
- The same call can be logged by both caller and callee, so we keep one record per (traceid, rpcid). We prefer a known response time, and the larger one, since the caller side includes the network. The earliest start time is kept.
- A negative `rt` means the response time is missing, so `duration_us` is null.
- Calls whose parent rpcid is not in the file are counted as orphans. The stitcher will mark those traces incomplete.
- Note: dedup holds the file in memory, so split a full-day file by hour before converting.

**RCAEval (RE2/RE3).** Uses the same columns RCAEval's own trace methods read: traceID, spanID, parentSpanID, serviceName, operationName, methodName, startTime, duration, and a status code column if present.
- Case folders `{benchmark}_{service}_{fault}_{instance}` also give the ground truth. `convert` writes it to `<output>.label.json` (root-cause service, fault, inject time) for the RCAEval adapter (E12).

**OTLP JSON.** This is the standard ExportTraceServiceRequest JSON from any OTel SDK. It accepts hex or base64 IDs, numeric or string enums, and one document per file or per line.
- Our own conversion to OTLP is lossless: non-OTLP IDs are hashed into valid OTLP IDs, and the originals are kept in `sta.*` attributes.

## Replayer details

- Spans are sent when they finish (start + duration), which is when a real SDK exports them. Spans of one trace therefore arrive spread out, and the quiet-window completion gets realistic input.
- Speed only compresses the gaps between spans; durations are never changed.
- `--rebase-to-now` shifts each trace as a unit to the current time, for freshness measurements (N1).
- Batches flush every `--flush-interval` seconds or every `--batch-size` spans.
- The summary reports how late each span was versus its schedule (`max_lag_s`, `p99_lag_s`). That is the E2 check that inter-arrival times match the original timing.
- HTTP sends retry on 5xx/429/network errors with backoff, and fail immediately on 401/403 (wrong token).

## Size limits and performance

- Measured on a laptop-class CPU: a 12 MB Alibaba CSV (294k rows, 40% duplicates) converts in about 6 s and replays at max speed in about 10 s. Memory is roughly 3 KB per span, so a 1M-span file needs about 3 GB; split a full day of Alibaba data by hour (or by trace-id range) before converting.
- OTLP JSON is about 10x larger than the CSV it came from, and JSONL is about half of OTLP. For uploads use `--max-spans-per-file 50000` so each object stays well under 100 MB.
- The adapter Lambda reads the whole object into memory. Give it 1 GB and a 5 minute timeout, and keep uploads under the limit above. Parquet is not supported inside the Lambda; convert locally first.
- The S3 event notification should use a prefix filter (`uploads/`, `live/`) so the Lambda's own `normalized/` writes never trigger it; the handler also skips them as a second guard.
- Normalized output keeps the raw key path: `uploads/a.json` becomes `normalized/uploads/a.jsonl`, so two uploads with the same file name in different folders never overwrite each other.

## Requirements covered

- **E2:** one span per call after dedup; timing at 1x/2x/10x is checked in `ReplayTests`.
- **E3:** the converter and presigned upload. `UploadEqualsReplayTests` checks that a converted file and a replay of the same data produce identical spans.
- **N4:** the adapter's output key is derived from the input key, so retries overwrite instead of duplicating. Bad files are reported per message (SQS partial batch response), and spans that fail validation are dropped and counted rather than passed on.
- **N6:** the ingest token is read from `--token` or `STA_INGEST_TOKEN` and never written to logs or files.

## Open questions for the team

1. **Larry:** does this span format work for reconstruction? Any fields missing?
2. **Matthew:**
   - Which header does the collector Lambda expect for the token? (Default here: `Authorization: Bearer <token>`, configurable with `--auth-header` / `--auth-scheme`.)
   - Does the dashboard API presign endpoint shape (`POST {filename, content_type}` → `{url, key}`) work for you?
3. **Adapter Lambda setup:** S3 event → SQS → this Lambda, with `ReportBatchItemFailures` on. Env: `NORMALIZED_PREFIX` (default `normalized/`), optional `NORMALIZED_BUCKET`, `BASE_EPOCH_US`. Handler: `traceingest.adapter_lambda.handler`.
