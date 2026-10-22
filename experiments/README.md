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
  "rcaeval":  {"cases": ["data/rcaeval/re2ob_*"], "methods": ["slowest_average", "longest_span", "critical_path_only"],
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

Until the real cases are downloaded, `python -m experiments.tests.make_cases data/rcaeval-synthetic`
builds five synthetic cases in the same layout, which is what the tests use. Numbers
from those are for checking the plumbing only.

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
