# Score v2 and MicroRCA on RCAEval

The same 240 RCAEval trace cases as the naive baselines (`../2026-10-23-naive-baselines/`)
and the v1 score (`../2026-11-10-diagnosis-score/`), now with:

- **MicroRCA**: `experiments/microrca.py`, the trace-only version (service call graph,
  p90-slowdown anomaly detection, personalized PageRank). Ran on all 240 cases.
- **Score v2**: `diagnosis/score_v2.py` through `experiments.diagnosis_method:score_v2_fast`,
  which skips the per-record schema check once a 1,000-record random sample of the case
  passes (see "Validation" below). Ran on 227 of 240 cases; the other 13 need more memory
  than the 8 GB machine has (listed below).

```bash
python -m experiments.run experiments/reported/2026-11-22-v2-and-microrca/config.json
```

MicroRCA takes about 12 s per case. v2 takes 2–3 minutes per Online Boutique case and
3–6 minutes per Train Ticket case, with peaks of 3–6 GB of memory; the whole run is
about 12 hours on one worker.

## Result

On the 227 cases every method completed (all 240 in brackets where it differs):

| method | acc@1 | acc@3 | MRR | true service not ranked |
|---|---|---|---|---|
| **score v2** | 0.53 | **0.70** | **0.66** | 16 |
| **MicroRCA** | **0.58** (0.60) | 0.64 (0.66) | 0.64 (0.66) | 24 |
| longest_span (best naive) | 0.34 (0.36) | 0.61 (0.63) | 0.49 (0.51) | 59 |
| critical_path_only | 0.23 (0.24) | 0.55 | 0.45 | 14 |
| slowest_average | 0.24 | 0.35 (0.34) | 0.39 | 14 |
| score v1 (tail-share-excess) | 0.14 (0.15) | 0.30 (0.31) | 0.22 (0.23) | 152 |

acc@1 / acc@3 per benchmark, same 227 cases:

| benchmark | v2 | MicroRCA | longest_span | critical_path_only | slowest_average | v1 |
|---|---|---|---|---|---|---|
| RE2 Online Boutique (90) | 0.27 / 0.59 | **0.70 / 0.83** | 0.08 / 0.47 | 0.07 / 0.53 | 0.36 / 0.46 | 0.06 / 0.29 |
| RE2 Train Ticket (77 of 90) | **0.88 / 0.94** | **0.88** / 0.92 | 0.68 / 0.86 | 0.40 / 0.79 | 0.17 / 0.30 | 0.34 / 0.47 |
| RE3 Online Boutique (30) | 0.00 / **0.20** | 0.00 / 0.00 | 0.00 / 0.10 | 0.00 / 0.03 | 0.00 / 0.00 | 0.00 / 0.00 |
| RE3 Train Ticket (30) | **0.97 / 0.97** | 0.00 / 0.00 | 0.60 / 0.93 | 0.50 / 0.50 | 0.33 / 0.50 | 0.00 / 0.20 |

## What it shows

- **Both beat every naive baseline.** v2 moves from the bottom (v1: 0.15 acc@1) to roughly
  level with the published method: MicroRCA is a little better at putting the culprit
  first, v2 is better at getting it into the top three and has the best MRR.
- **They win in different places.** v2 finds 29 of 30 RE3 Train Ticket code-level faults;
  the next best is longest_span with 18, and MicroRCA and v1 find none. v2's error-rate
  signal is the likely reason, since these faults change errors more than latency. MicroRCA is far better on RE2
  Online Boutique (0.70 vs 0.27), where v2 still blames the caller: a delay injected in
  front of a service shows up in its caller's span, and v2 attributes it to the caller.
  MicroRCA's graph walk moves it downstream. Combining the two ideas is the obvious next
  step.
- **RE3 Online Boutique stays near zero for everyone.** 12 of its 30 cases inject into
  services with no spans at all (see the baseline summary), and the rest are code faults
  that barely change timing or errors in the traces.

## Validation

v2 rows use sampled schema validation (`score_v2_fast`): a random 1,000 records per case
are checked against the canonical schema, and if all pass the per-record check is skipped.
All 227 cases passed their sample. Because the full v2 excludes a whole trace when one of
its records is invalid, the fast version matches it only if every record is valid, which
the sample makes likely but does not prove. Spot check with the full, unchanged `score_v2`:

| case | full v2 | fast v2 |
|---|---|---|
| re3ob_emailservice_f1_1 | rank 5, top 3 checkoutservice; productcatalogservice; frontend | same |
| re2ob_currencyservice_delay_1 | rank 5, top 3 frontendservice; checkoutservice; emailservice | same |

## Not run

13 RE2 Train Ticket cases ran out of memory for v2 even with a lean loader that keeps one
copy of the case in memory: `re2tt_ts-auth-service_cpu_1`, `_delay_1`, `_mem_1`, `_mem_2`;
`re2tt_ts-order-service_disk_1`, `_loss_1`, `_socket_1`; `re2tt_ts-route-service_disk_1`,
`_loss_1`, `_socket_1`; `re2tt_ts-train-service_cpu_1`, `_socket_2`;
`re2tt_ts-travel-service_loss_2`. They need a machine with more than 8 GB; until then the
comparison above uses the 227 cases all methods share. MicroRCA puts the culprit first in
all 13 of the missing cases, so the shared-case table understates MicroRCA a little (0.58
instead of its 0.60 on all 240); v2 is 0.88 on the RE2 Train Ticket cases it did run, so
it would likely gain about as much, but that is not measured.

## Files

- `rcaeval_results.csv`: one row per case and method (MicroRCA 240, v2 227)
- `rcaeval_summary.json`: acc@1/3/5, MRR per fault type and benchmark, and the 13 failed cases
- `details.csv`: run time, peak memory, validation mode and loader per case
