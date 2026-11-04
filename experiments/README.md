# experiments: runner, baselines, metrics, figures

Owner: Anukrithi (Sprint 6)

One command regenerates every number and figure in the report:

```bash
pip install -r experiments/requirements.txt        # matplotlib
python -m experiments.run experiments/config.json  # from the repo root
```

Results land in `experiments/results/<timestamp>/` (and `results/latest/`):

| file | what |
|---|---|
| `config.json` | copy of the config that produced this run |
| `rcaeval_results.csv` | one row per (case, method): where the true service ranked |
| `rcaeval_summary.json` | accuracy@1, accuracy@3, MRR per method, and per fault type |
| `freshness.json` | staleness/lag numbers pulled from the dashboard's `/api/pipeline` |
| `usability_summary.json` | timing study summary |
| `cost.csv` | measured monthly cost rows, copied in |
| `figures/*.png` | the charts |
| `summary.md` | the key numbers in one page |

Every step is skipped when its input is missing, so the runner can be used from
day one and grows as data arrives.

## Config

```json
{
  "replay":   [{"input": "data/alibaba_hour.csv", "speed": "max", "url": "https://...on.aws/", "token_env": "STA_INGEST_TOKEN"}],
  "rcaeval":  {"cases": ["data/rcaeval/re2ob_*", "data/rcaeval/re2tt_*", "data/rcaeval/re3ob_*", "data/rcaeval/re3tt_*"],
               "workers": 2, "methods": ["slowest_average", "longest_span", "critical_path_only"],
               "extra_methods": ["diagnosis.score:rank_services"]},
  "freshness": {"dashboard_url": "http://127.0.0.1:8000", "file": "experiments/freshness.json"},
  "usability_log": "experiments/usability/timing_log.csv",
  "cost_csv": "experiments/cost.csv"
}
```

`extra_methods` is how the real bottleneck score and MicroRCA plug in: any
`module:function` with the signature `fn(normal_traces, anomalous_traces) -> [service, ...]`
(best guess first) is scored exactly like the built-ins. The built-ins are the three
naive heuristics from E9.

## RCAEval

Each case folder (`{benchmark}_{service}_{fault}_{instance}`) is read with the ingest
package's RCAEval reader, split at `inject_time.txt` into normal and anomalous traces,
and every method is asked for a ranking. Scores follow RCAEval's definitions:
accuracy@k is the fraction of cases where the true service is in the top k, MRR is
the mean of 1/rank.

Getting the data: the cases with traces are RE2 and RE3 for Online Boutique and
Train Ticket (240 cases, about 2.6 GB as Parquet; RE1 and Sock Shop have no traces).

```bash
pip install -r experiments/requirements.txt
python -m experiments.get_rcaeval            # -> data/rcaeval/<case>/traces.parquet + inject_time.txt
```

`workers` in the config runs cases in parallel; a Train Ticket case is about 1.1M
spans, so two workers roughly halve the 45 minutes the full set takes.

The tests use five synthetic cases in the same layout
(`python -m experiments.tests.make_cases data/rcaeval-synthetic`); those numbers only
check the plumbing.

### Results so far

`reported/` keeps the runs quoted in the workbook and report, each with its config,
`rcaeval_results.csv` (rank per case and method), `rcaeval_summary.json` and figures.

| run | cases | method | acc@1 | acc@3 | MRR |
|---|---|---|---|---|---|
| 2026-10-23 naive baselines | 240 (RE2-OB 90, RE2-TT 90, RE3-OB 30, RE3-TT 30) | slowest_average | 0.24 | 0.34 | 0.39 |
| | | longest_span | 0.36 | 0.63 | 0.51 |
| | | critical_path_only | 0.24 | 0.55 | 0.45 |

Two things to keep in mind when reading those: on a single-root shop the front end is on
every critical path and takes rank 1 in 114 of the 120 Online Boutique cases for the
critical-path heuristic, and 12 of the 30 RE3 Online Boutique cases inject into adservice
or cartservice, which have no spans in the traces at all, so no trace-only method can find
them (the other 18 are code-level faults in emailservice and currencyservice that change
errors more than timing).

## Freshness on one machine

`local_pipeline.py` runs the whole pipeline locally and measures freshness (N1)
before the AWS deployment is up. The replayer sends spans at 1x to a small local
collector (it checks the token and stores each batch, like the collector Lambda);
from there it is the real code: the adapter Lambda's handler, the reconstruction
store and worker on PostgreSQL, and the dashboard's pipeline numbers.

```bash
docker compose up -d postgres            # repo root
python -m experiments.local_pipeline data/rcaeval/re2ob_checkoutservice_cpu_1 --seconds 180 --keep-values
```

First run (Nov 2, `reported/2026-11-02-local-freshness/`), one RCAEval Online
Boutique case at 1x for 3 minutes: 46,409 spans, 2,827 traces, all settled, no
duplicates or adapter failures.

| | p50 | p95 | p99 |
|---|---|---|---|
| staleness (span end → normalized, the Pipeline view's number) | 0.61 s | 1.10 s | 1.23 s |
| adapter lag (collector → normalized) | 0.15 s | 0.34 s | |
| settle (last span end → trace settled, includes the 5 s quiet window) | 6.48 s | 8.16 s | 9.31 s |

The 60 s target is met with a wide margin locally. On AWS the SQS hop and Lambda
start-up add to this, so these numbers are a lower bound. The AWS measurement is
the same replay sent to the collector's Function URL (`python -m traceingest replay
... --url <collector URL> --rebase-to-now`), with the runner's freshness step pulling
the numbers from the deployed dashboard (`"freshness": {"dashboard_url": ...}` in
`config.json`). `--keep-values` stores every staleness value so `figures.py` can
draw the CDF.

### Alibaba production traces

Run on Nov 2 (`reported/2026-11-02-alibaba-local/`), samples of the first 5-minute
file of the Alibaba v2021 release cut with `ingest/tools/alibaba_sample.py` (commands
in `REPRODUCE.md`), replayed at 1x for the full 5 minutes:

| sample | spans | traces | staleness p50 / p95 / p99 | settled by quiet window |
|---|---|---|---|---|
| 2%, about 210 spans/s | 62,390 | 1,441 | 0.51 / 1.19 / 2.00 s | all |
| 5%, about 510 spans/s | 154,264 | 3,564 | 31.4 / 75.8 / 79.5 s | 428 (rest hit the 60 s timeout) |

At 2% the target is met as easily as on RCAEval. At 5% one process falls behind:
ingest (validating and storing each record) takes about 2.1–2.4 ms per span, so the
local harness tops out around 420–470 spans/s. The summary breaks the time down by
stage; on AWS each batch is its own Lambda, so this is a limit of the one-process
harness, and the per-span ingest cost is the thing to watch in the throughput tests.

## Usability study

`usability/protocol.md` is the procedure for the timed Jaeger-versus-dashboard
comparison. The kit does the setup:

```bash
python -m experiments.usability.episodes prepare data/rcaeval    # episodes A-D, task sheet, answer key
python -m experiments.usability.session P1                       # one participant: load, read, time, log
python -m experiments.usability.usability_stats experiments/usability/timing_log.csv
```

`episodes.py` cuts four RCAEval delay faults (two Online Boutique, two Train Ticket)
to 5 minutes either side of the fault and picks the busiest endpoint that clearly got
slower and is not served by the culprit. `load` moves an episode so it ends half a
minute ago and sends it to Jaeger over OTLP and/or into the dashboard's data folder.
`schedule.csv` (from `schedule.py`) counterbalances tool order and which episodes go
to which tool. `session.py` times each answer and checks it against the answer key,
so the facilitator never has to look at it. Jaeger setup is in `baselines/jaeger/`.

## Cost

`experiments/cost.csv` is filled in by hand from the AWS bill (`name,usd_month`), one
row per configuration (idle, during experiments, always-on comparison).

## Tests

```bash
python -m unittest discover -s experiments/tests -t .
```
