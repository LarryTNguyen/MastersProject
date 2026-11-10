# Reproducing the reported numbers

Every number we quote in the workbook and the report comes from one of the commands
below. Saved runs are in `experiments/reported/<date>-<name>/`, each with the exact
command or config that produced it.

## Setup

```bash
docker compose up -d postgres                      # PostgreSQL 16 on :55432 with trace_schema/schema.sql applied
python -m pip install -r trace_schema/requirements.txt -r reconstruction/requirements.txt \
                      -r dashboard/requirements.txt -r experiments/requirements.txt
export TEST_DATABASE_URL=postgresql://trace:trace@localhost:55432/trace
```

Python 3.12. The browser tests also need `pip install playwright && python -m playwright install chromium`.

## At a glance

| number | saved in | command |
|---|---|---|
| 221 tests passing | CI (`.github/workflows/tests.yml`) | [Tests](#tests) |
| naive baselines on 240 RCAEval cases (best acc@1 0.36) | `experiments/reported/2026-10-23-naive-baselines/` | [RCAEval baselines](#rcaeval-baselines) |
| diagnosis score v1 on the same 240 cases (acc@1 0.15) | `experiments/reported/2026-11-10-diagnosis-score/` | [RCAEval baselines](#rcaeval-baselines) |
| local freshness on RCAEval, p95 1.10 s | `experiments/reported/2026-11-02-local-freshness/` | [Local freshness](#local-freshness-rcaeval) |
| local freshness on Alibaba production traces, p95 1.19 s | `experiments/reported/2026-11-02-alibaba-local/` | [Alibaba](#alibaba-production-traces) |
| usability study times and accuracy | `experiments/usability/timing_log.csv` | [Usability](#usability-study) |

## Tests

CI runs these on every push and pull request. Locally, with `TEST_DATABASE_URL` set
(the PostgreSQL tests are skipped without it, the browser tests without Playwright):

```bash
python -m unittest discover -s diagnosis/tests -t .               # 19
(cd ingest && python -m unittest discover -s tests -t .)           # 52
(cd reconstruction && python -m unittest discover -s tests -t .)   # 34
(cd dashboard && python -m unittest discover -s tests -t .)        # 81 (8 PostgreSQL, 6 browser)
python -m unittest discover -s experiments/tests -t .              # 35
```

## RCAEval baselines

```bash
python -m experiments.get_rcaeval                                  # the 240 cases with traces, about 2.6 GB
python -m experiments.run experiments/reported/2026-10-23-naive-baselines/config.json
```

About 45 minutes with two workers. `experiments/results/latest/summary.md` should
match `reported/2026-10-23-naive-baselines/summary.md` exactly; the methods are
deterministic. The diagnosis score has its own config and takes about 5 hours with
two workers:

```bash
python -m experiments.run experiments/reported/2026-11-10-diagnosis-score/config.json
```

## Local freshness (RCAEval)

```bash
python -m experiments.local_pipeline data/rcaeval/re2ob_checkoutservice_cpu_1 --seconds 180 --keep-values
```

Replays 3 minutes at 1x through the local collector, the adapter Lambda's handler,
reconstruction on PostgreSQL and the dashboard's pipeline numbers. Timing numbers
move a little from run to run and machine to machine; span and trace counts do not.

## Alibaba production traces

The v2021 microservices release is split into 5-minute files. We use the first one:

```bash
mkdir -p data/alibaba2021 && cd data/alibaba2021
curl -O https://aliopentrace.oss-cn-beijing.aliyuncs.com/v2021MicroservicesTraces/MSCallGraph/MSCallGraph_0.tar.gz
tar xzf MSCallGraph_0.tar.gz                                       # MSCallGraph_0.csv, 1.3 GB, 6.1M rows
cd ../..
python ingest/tools/alibaba_sample.py data/alibaba2021/MSCallGraph_0.csv --list      # entry services by traffic
python ingest/tools/alibaba_sample.py data/alibaba2021/MSCallGraph_0.csv \
       -o data/alibaba2021/samples/top1_2pct.csv --entry top1 --fraction 0.02
python ingest/tools/alibaba_sample.py data/alibaba2021/MSCallGraph_0.csv \
       -o data/alibaba2021/samples/top1_5pct.csv --entry top1 --fraction 0.05
```

The sample keeps a hashed share of the traces that enter through the busiest entry
service, so the same command always gives the same rows (2%: 80,346 rows, 1,441
traces; 5%: 202,166 rows, 3,564 traces).

```bash
python -m experiments.local_pipeline data/alibaba2021/samples/top1_2pct.csv --seconds 310 --settle-wait 20 --keep-values
python -m experiments.local_pipeline data/alibaba2021/samples/top1_5pct.csv --seconds 310 --settle-wait 20 --keep-values
```

The 2% sample is the freshness number; the 5% sample is where one process stops
keeping up (see the saved summary).

## Freshness on AWS

Once the collector, the queue and the adapter Lambda are deployed, replay 3 minutes
at 1x into the collector and read the stamps back from the normalized bucket (before
Aurora) or from the dashboard (after). The exact commands are in
`experiments/README.md`, "Freshness on AWS":

```bash
python -m experiments.aws_freshness files experiments/results/aws-normalized --since <replay start>
python -m experiments.aws_freshness dashboard <dashboard Function URL>
```

## Usability study

Procedure in `experiments/usability/protocol.md`. In short:

```bash
python -m experiments.usability.episodes prepare data/rcaeval
python -m experiments.usability.session P1                         # once per participant
python -m experiments.usability.usability_stats experiments/usability/timing_log.csv
```

## Lambda archives

```bash
(cd dashboard && python tools/build_lambda.py)    # dashboard/dist/dashboard-lambda.zip
(cd ingest && python tools/build_lambda.py)       # ingest/dist/adapter-lambda.zip
```
