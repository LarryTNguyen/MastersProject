# AWS deployment (us-west-1)

What is deployed, how the pieces connect, and how to redeploy or change them. Written
from the live account on November 21, 2026. Secrets (database password, ingest
token) are not in this file; see "Secrets" below.

```
replayer / app ──POST /v1/traces──► mal-collector (Function URL, bearer token)
                                          │ live/YYYY/MM/DD/<id>.json
dashboard Upload panel ──presigned PUT──► │ uploads/YYYY/MM/DD/<id>-<name>
                                          ▼
                              S3 mal-tracing-raw-us-west-1
          live/, uploads/ ──S3 event──► SQS mal-tracing-processing-queue ──► mal-dataset-adapter
                                                    (DLQ mal-tracing-dlq)        │ normalized/<same path>.jsonl
          normalized/ ─────S3 event──► SQS mal-reconstruction-queue ─────► mal-reconstrution ──► Aurora trace_v1
                                                    (DLQ mal-reconstruction-dlq)
          normalized/uploads/ ─EventBridge rule mal-diagnosis-on-upload─► mal-diagnosis ──► Aurora service_window_results
          schedule mal-reconstruction-settle (rate 1 min, DISABLED by default) ──► mal-reconstrution
                                                                       Aurora ◄── dashboard-lambda (Function URL)
```

## Lambda functions

| function | handler | runtime | memory / timeout | trigger | in VPC | env (names only) |
|---|---|---|---|---|---|---|
| `mal-collector` | `traceingest.collector_lambda.handler` | Python 3.12, arm64 | 256 MB / 10 s | Function URL (auth NONE, bearer token checked in code) | no | `RAW_BUCKET`, `RAW_PREFIX=live/`, `INGEST_TOKEN` |
| `mal-dataset-adapter` | `traceingest.adapter_lambda.handler` | Python 3.13, x86_64 | 1024 MB / 300 s | SQS `mal-tracing-processing-queue`, batch 10, report batch item failures on | no | `NORMALIZED_PREFIX=normalized/`, `TRACE_RUN_ID` |
| `mal-reconstrution` | `trace_reconstruction.lambda_handler.handler` | Python 3.12, arm64 | 512 MB / 60 s | SQS `mal-reconstruction-queue`, batch 10, report batch item failures on; EventBridge rule `mal-reconstruction-settle` | yes | `DATABASE_URL`, `NORMALIZED_PREFIX` |
| `mal-diagnosis` | `diagnosis.lambda_handler.handler` | Python 3.12, arm64 | 512 MB / 60 s | EventBridge rule `mal-diagnosis-on-upload` | yes | `DATABASE_URL`, `NORMALIZED_PREFIX`, `ASSUME_SYNCHRONOUS=true`, `DATA_LABEL`, `SYNTHETIC_AS_MEASURED` |
| `dashboard-lambda` | `app.lambda_handler.handler` | Python 3.12, arm64 | 512 MB / 30 s | Function URL (auth NONE) | yes | `DATABASE_URL`, `STORE=trace_v1`, `RAW_BUCKET`, `RAW_PREFIX=uploads/`, optional `RUN_ID` |

The function name `mal-reconstrution` is misspelled in AWS; renaming means creating a
new function, so it stays.

VPC: the default VPC (`vpc-0954be0da176d65e0`), subnets in us-west-1a and us-west-1c,
security group `sg-005e91c6586e85336`, which also guards Aurora. An S3 gateway endpoint
(`s3-gateway-us-west-1`) lets the in-VPC Lambdas read S3. There is no NAT gateway on
purpose: it would cost about $30 a month while idle. The collector and adapter stay
outside the VPC because they never talk to the database.

## Queues

| queue | visibility timeout | dead-letter queue |
|---|---|---|
| `mal-tracing-processing-queue` | 30 min (6x the adapter timeout) | `mal-tracing-dlq` after 3 receives |
| `mal-reconstruction-queue` | 6 min | `mal-reconstruction-dlq` (14-day retention) after 5 receives |

## S3 bucket `mal-tracing-raw-us-west-1`

| prefix | written by | event |
|---|---|---|
| `uploads/` | dashboard presigned uploads | `mal-trace-upload-event` → processing queue |
| `live/` | `mal-collector` | `mal-live-batches` → processing queue |
| `normalized/` | adapter | `normalized-to-reconstruction` → reconstruction queue; EventBridge (on) for the diagnosis rule |

## EventBridge rules

- `mal-diagnosis-on-upload`: S3 "Object Created" under `normalized/uploads/` → `mal-diagnosis`
  with input `{"bucket": ..., "key": ...}`. Each uploaded dataset is diagnosed once, as a
  whole file. Live batches are not diagnosed per file (one batch is about a second of
  traffic, too few traces per window); a windowed job over the live run is future work.
- `mal-reconstruction-settle`: every minute, invokes reconstruction with
  `source=aws.events`, which settles traces whose quiet window has passed even when no new
  file arrives. **Disabled by default**, because a call every minute keeps Aurora from
  pausing (about $40–50 a month at 0.5 ACU). Turn it on for a demo or an experiment and off
  afterwards:

  ```bash
  aws events enable-rule  --name mal-reconstruction-settle
  aws events disable-rule --name mal-reconstruction-settle
  ```

## Aurora

Cluster `mal-trace-dev`, Aurora PostgreSQL 16.15, Serverless v2 at 0–1 ACU (pauses when
idle), one writer in us-west-1a, private (no public access). `trace_schema/schema.sql`
is applied. Database `trace`.

## Runs

The adapter's `TRACE_RUN_ID` puts everything it normalizes into one run, so a replay sent
in many small batches shows up as one run on the dashboard. Use a new UUID per experiment:

```bash
aws lambda update-function-configuration --function-name mal-dataset-adapter \
  --environment "Variables={NORMALIZED_PREFIX=normalized/,TRACE_RUN_ID=$(python3 -c 'import uuid;print(uuid.uuid4())')}"
```

The dashboard shows the newest run unless `RUN_ID` is set on `dashboard-lambda`.

## Secrets

- `DATABASE_URL` is set directly on the three in-VPC Lambdas (encrypted at rest by Lambda).
  Reading it from Secrets Manager at run time would need a paid VPC endpoint.
- The ingest token lives in SSM Parameter Store as `/mal/ingest-token` (SecureString) and
  in `mal-collector`'s `INGEST_TOKEN`. To set or rotate it (CloudShell, us-west-1):

  ```bash
  TOKEN=$(openssl rand -hex 24)
  aws ssm put-parameter --name /mal/ingest-token --type SecureString --value "$TOKEN" --overwrite
  aws lambda update-function-configuration --function-name mal-collector \
    --environment "Variables={RAW_BUCKET=mal-tracing-raw-us-west-1,RAW_PREFIX=live/,INGEST_TOKEN=$TOKEN}"
  unset TOKEN
  ```

  To use it: `export STA_INGEST_TOKEN=$(aws ssm get-parameter --name /mal/ingest-token --with-decryption --query Parameter.Value --output text)`.
  Until it is set, the collector answers 401 to everything.

## Redeploying code

Function URLs: `aws lambda get-function-url-config --function-name <name> --query FunctionUrl`.

```bash
# collector and adapter: same zip, different handler
(cd ingest && python tools/build_lambda.py)
aws lambda update-function-code --function-name mal-dataset-adapter --zip-file fileb://ingest/dist/adapter-lambda.zip
aws lambda update-function-code --function-name mal-collector       --zip-file fileb://ingest/dist/adapter-lambda.zip

# dashboard (arm64 by default)
(cd dashboard && python tools/build_lambda.py)
aws lambda update-function-code --function-name dashboard-lambda --zip-file fileb://dashboard/dist/dashboard-lambda.zip
```

`mal-reconstrution` and `mal-diagnosis` are zips of `trace_reconstruction/` or
`diagnosis/` + `reconstruction/trace_reconstruction/`, plus `trace_schema/` and Linux
(aarch64) wheels of psycopg[binary] and jsonschema. There is no build script for them yet.

## Checks

```bash
# collector up and refusing requests without a token (expect 401)
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{}' "$COLLECTOR_URL/v1/traces"

# end to end through the upload path: copy any uploaded file to a new key, wait a minute
aws s3 cp s3://mal-tracing-raw-us-west-1/uploads/<file> s3://mal-tracing-raw-us-west-1/uploads/<date>/e2e-<file>
curl -s "$DASHBOARD_URL/api/runs"          # newest run = TRACE_RUN_ID, with traces
curl -s "$DASHBOARD_URL/api/rankings"      # diagnosis rankings for the upload
```

Freshness on AWS (research question 1): `experiments/README.md`, "Freshness on AWS".
Replay to the collector URL with `--rebase-to-now`; with the settle schedule on, the
dashboard's `/api/pipeline` gives the numbers directly.

## Known gaps

- Log groups keep logs forever; 7 days was the plan.
- The dashboard Function URL is public with no login, including the upload panel.
- No CloudWatch alarms on the dead-letter queues yet.
- No infrastructure-as-code; this file is the record of the setup.
