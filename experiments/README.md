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
every critical path and often takes rank 1, and 18 of the 30 RE3 Online Boutique cases
inject into adservice or cartservice, which have no spans in the traces at all, so no
trace-only method can find them.

## Usability study

`usability/protocol.md` is the procedure for the timed Jaeger-versus-dashboard
comparison; fill in `usability/timing_log.csv` as sessions happen. Jaeger setup is in
`baselines/jaeger/`.

## Cost

`experiments/cost.csv` is filled in by hand from the AWS bill (`name,usd_month`), one
row per configuration (idle, during experiments, always-on comparison).

## Tests

```bash
python -m unittest discover -s experiments/tests -t .
```
