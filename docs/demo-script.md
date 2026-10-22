# Demo script

About eight minutes. Works offline with the demo data; swap in the Alibaba replay
for the real thing.

## Before

```bash
cd dashboard && ./tools/demo.sh          # demo data, server, browser
```
Have these tabs open: the dashboard, the AWS billing page (or `experiments/results/latest/figures/cost.png`),
and Jaeger with the same data if the comparison is part of the demo.

## 1. The problem (30 s)

"Checkout got slow this morning. Here is what an engineer sees today." Show Jaeger:
a list of traces, open one, scroll the waterfall. "Which service? You'd open a few
of these and guess."

## 2. Overview (1 min)

Dashboard → Overview. Point at the services table: p99 column, the bars. "Same data.
Three services look slow, but two of them are just waiting on the third." Point at
the ranking panel: "The pipeline already ranked it: paymentservice, score 0.91."

## 3. Evidence (2 min)

Bottlenecks tab. Walk the top entry's evidence line: critical-path time in slow
traces versus normal, self-time ratio, error rate. Then the slow-versus-normal table:
"In the slow 10% of requests paymentservice adds 230 ms of its own time; every other
service is flat. That's the whole story in one table."

Click paymentservice → the slowest traces → open one. Waterfall: "The long bar is
payment's own time, not its children; the thin line under it is self time."

Mention the second window: "When nothing stands out, it says so instead of guessing."

## 4. Where the time goes (1 min)

Flame graph tab, endpoint = checkout, traces = slowest 10%. "Many traces merged. Width
is time. One wide box." Click it to zoom, click the top bar to reset.

## 5. It is live, and it is cheap (2 min)

Pipeline tab: staleness p95, ingest rate, incomplete traces. "Spans show up here
within seconds of finishing, and the pipeline tells you when it is unhealthy."

Then the cost figure or the billing page: "Nothing runs when no traces flow. Idle
cost last month: about a dollar."

## 6. Upload (30 s)

Upload tab, drop a converted file. "Anyone with an OpenTelemetry app, or a dataset,
can use it with no setup."

## 7. Results (1 min)

`experiments/results/latest/figures/rcaeval_accuracy.png`: "On RCAEval's labeled
failures, accuracy@1 against the heuristics and MicroRCA." And the usability chart:
"Time to a correct diagnosis, Jaeger versus the dashboard."

## If something breaks

- Dashboard empty: `python tools/make_demo_data.py data --now`, reload.
- Pipeline tab empty: the demo data must be generated with `--now`.
- Jaeger down: skip step 1, say what engineers do today instead.
- Keep the screenshots in `docs/screenshots/` as a fallback.
