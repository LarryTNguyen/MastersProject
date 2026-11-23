# diagnosis

Ranks services for slow endpoints (`python -m diagnosis`) and publishes
`trace_v1.service_window_results` for the dashboard Bottlenecks view
(`python -m diagnosis.publisher`).

## AWS Lambda

```powershell
cd diagnosis
python tools/build_lambda.py
```

Handler: `diagnosis.lambda_handler.handler`

- Direct Test event: `{"bucket":"mal-tracing-raw-us-west-1","key":"normalized/uploads/...jsonl"}`
- Or SQS ← S3 `normalized/` (use a **delivery delay** of ~90–120 s so reconstruction can settle first)

Env: `DATABASE_URL`, `ASSUME_SYNCHRONOUS=true`, `SYNTHETIC_AS_MEASURED=true`,
`NORMALIZED_PREFIX=normalized/`, same VPC as Aurora as the other Lambdas.
