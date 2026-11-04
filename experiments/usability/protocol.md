# Usability study: Jaeger versus the dashboard

Goal: measure how long it takes a person to name the service responsible for a slow
episode, and how often they are right, using Jaeger (today's manual approach) and
our dashboard, on the same traces.

## Episodes

Four RCAEval delay faults, each cut to 5 minutes before and 5 minutes after the
fault starts:

| episode | benchmark | case |
|---|---|---|
| A | Online Boutique | `re2ob_checkoutservice_delay_1` |
| B | Online Boutique | `re2ob_recommendationservice_delay_1` |
| C | Train Ticket | `re2tt_ts-route-service_delay_1` |
| D | Train Ticket | `re2tt_ts-travel-service_delay_1` |

For each one the kit picks the busiest endpoint that clearly got slower (p95 at least
1.5x) and is not served by the culprit, so the question never contains the answer.

## Setup, once

```bash
python -m experiments.get_rcaeval --suites re2ob re2tt   # if data/rcaeval is not there yet
python -m experiments.usability.episodes prepare data/rcaeval
python -m experiments.usability.schedule 8 > experiments/usability/schedule.csv
```

`prepare` writes `experiments/usability/episodes/` (not committed): the spans for
each episode, `task_sheet.md` with the sentence to read, and `answer_key.json`.
Don't open the answer key in front of a participant; the session script checks
answers for you.

Start the two tools:

```bash
cd baselines/jaeger && docker compose up -d           # Jaeger UI on :16686, OTLP on :4318
cd dashboard && DATA_DIR=../experiments/usability/episodes/live_dashboard uvicorn app.main:app --port 8000
```

The dashboard notices new files in its data folder by itself. If the scoring step's
`rankings.json` is available for an episode, copy it into the same folder; without
it, participants use the compare, waterfall and flame views.

## Schedule

`schedule.csv` gives each participant four episodes, two per tool, and repeats every
four participants:

- half start with Jaeger, half with the dashboard, and the tools alternate
- each episode is done with both tools equally often
- each tool gets one Online Boutique and one Train Ticket episode per person

Participants should not have seen the datasets before. Team members do not count.

## Running a session

```bash
python -m experiments.usability.session P3
```

The script walks through the participant's four episodes. For each one it:

1. says which tool to open, and for a Jaeger episode waits while you restart Jaeger
   (it keeps everything in memory, so this clears the previous episode);
2. loads the episode, shifted in time so it ends half a minute ago, and prints the
   sentence to read aloud, e.g. *"Requests to "frontend" on frontendservice became
   slow around 14:32. Which service is most likely responsible?"*;
3. starts the timer when you press Enter (when the participant touches the mouse),
   and stops it when you type the service they named, or `gave_up`;
4. asks for one line on what they looked at (the explanation check, not timed).

No hints. Each episode is one row in `timing_log.csv`:

| column | meaning |
|---|---|
| participant | P1, P2, … |
| tool | `jaeger` or `dashboard` |
| episode | A–D |
| order | 1–4, the position of this episode in the session |
| seconds | time to answer |
| answer | service named, or `gave_up` |
| correct | 1 if the answer matches the culprit, else 0 (names compared loosely) |
| notes | what they looked at |

To load an episode by hand instead (`session.py --no-load`):
`python -m experiments.usability.episodes load B --tool jaeger`.

## Results

```bash
python -m experiments.usability.usability_stats experiments/usability/timing_log.csv
```

- Median and p25–p75 time per tool, accuracy per tool, and the per-participant
  difference (dashboard minus Jaeger) so the comparison is paired.
- With fewer than 8 participants, report the numbers without significance claims.
