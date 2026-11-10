# The diagnosis ranking on RCAEval (first run)

The `diagnosis` package's ranking (`tail-share-excess-v1`, as on main on Nov 3)
scored on the same 240 RCAEval trace cases as the naive baselines, through
`experiments/diagnosis_method.py` (how it is called is described there). Settings
are the package defaults: 300 s windows, 10 traces per window, slow = 1.5x the
window median, 5 ms minimum excess, synchronous critical-path estimate. Windows that
overlap the fault are combined, each service's score weighted by the window's
number of slow traces.

```bash
python -m experiments.run experiments/reported/2026-11-10-diagnosis-score/config.json
```

About 5 hours with two workers on a 2-core machine (roughly 60 s per Online
Boutique case and 110 s per Train Ticket case, peak 4.8 GB per worker).

## Result

| method | acc@1 | acc@3 | MRR | true service not ranked |
|---|---|---|---|---|
| slowest_average | 0.24 | 0.34 | 0.39 | 14 |
| longest_span | **0.36** | **0.63** | **0.51** | 59 |
| critical_path_only | 0.24 | 0.55 | 0.45 | 14 |
| tail_share_excess (diagnosis v1) | 0.15 | 0.31 | 0.23 | 152 |

acc@1 / acc@3 per benchmark:

| benchmark | slowest_average | longest_span | critical_path_only | tail_share_excess |
|---|---|---|---|---|
| RE2 Online Boutique (90) | 0.36 / 0.46 | 0.08 / 0.47 | 0.07 / 0.53 | 0.06 / 0.29 |
| RE2 Train Ticket (90) | 0.18 / 0.29 | 0.68 / 0.88 | 0.40 / 0.77 | 0.36 / 0.48 |
| RE3 Online Boutique (30) | 0.00 / 0.00 | 0.00 / 0.10 | 0.00 / 0.03 | 0.00 / 0.00 |
| RE3 Train Ticket (30) | 0.33 / 0.50 | 0.60 / 0.93 | 0.50 / 0.50 | 0.00 / 0.20 |

As it stands, v1 is below every naive baseline. This is a first measurement of a
basic version, and it shows exactly where the score loses cases, which is the
useful part.

## Where it loses cases

1. **No ranking for the true service in 152 of 240 cases.** v1 only lists a service
   whose critical-path time grew by at least 5 ms *and* whose critical-path share
   grew. Everything else is left out, so acc@3 and MRR cannot recover the case even
   when the culprit was second-best. Only 23% of the endpoint windows during a fault
   produced any ranking (1,907 of 8,224); the rest were "no separable tail" or "no
   service has sufficient excess".

2. **The caller takes the blame in Online Boutique.** `frontendservice` is ranked
   first in 75 of the 90 RE2-OB cases (21 of 30 in RE3-OB). RCAEval's delay, loss and
   resource faults slow the callee down, but on the critical path the extra time
   shows up in the frontend's own span around the call, so it counts as frontend
   excess. The critical-path-only baseline has the same problem (front end first in
   114 of 120 OB cases). Checked on `re2ob_currencyservice_delay_1`: after the
   fault, frontend's `Convert` and `GetSupportedCurrencies` spans and checkout's
   `Convert` span each add about 200 ms of critical-path time, while
   currencyservice's own spans do not change (0.16 ms before and after). The delay
   sits between the caller's span and the callee's span. RCAEval spans carry no span
   kind, so a rule like "the gap between a span and its only child from another
   service goes to that child's service" would move this time to the culprit.

3. **Half of the Train Ticket traces are excluded.** 49% of RE2-TT traces and 33% of
   RE3-TT traces fail the "children contained in the parent's interval" check
   (Online Boutique: under 1%), so the method sees only part of the traffic there.
   Train Ticket's Java services make asynchronous calls and their clocks differ
   slightly between containers, so a child can start or end a few ms outside its
   parent. A small tolerance, or clipping children to the parent, would keep these.

4. **RCAEval intervals count as derived, not measured.** RCAEval gives each span a
   start and a duration, so the shared schema labels its end time DERIVED, and the
   diagnosis code only accepts MEASURED intervals: run unchanged, it excludes every
   RCAEval trace. The evaluation passes DERIVED as MEASURED (`ACCEPT_DERIVED` in
   `diagnosis_method.py`). The same applies to anything replayed with
   `--rebase-to-now`, which the schema also calls DERIVED, so the demo and the AWS
   runs hit this too. Worth a team decision on whether start + duration from a
   tracer is good enough for the diagnosis.

5. **RE3 code-level faults** mostly change errors rather than timing; no timing-only
   method does well there (see the baselines), so this is expected.

## Files

- `rcaeval_results.csv`: one row per case: where the true service ranked, top 3
- `rcaeval_summary.json`: acc@1/3/5, MRR, per fault type and per benchmark
- `details.csv`: per case, traces excluded, windows during the fault, windows with
  a ranking, and the diagnosis notes
- baselines for comparison: `../2026-10-23-naive-baselines/`
