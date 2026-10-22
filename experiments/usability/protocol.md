# Usability study: Jaeger versus the dashboard

Goal: measure how long it takes a person to name the service responsible for a slow
episode, and how often they are right, using Jaeger (today's manual approach) and
our dashboard, on the same traces.

## Setup

- Load one dataset with a known bottleneck into both tools (`baselines/jaeger/load.sh`
  and the dashboard's data folder). Use an episode where the ground truth is known:
  an RCAEval case, or an Astronomy Shop run with a failure flag on.
- Prepare 4 episodes (A–D). Each participant sees two in Jaeger and two in the
  dashboard. Alternate which tool comes first between participants so learning
  effects cancel out: participant 1 = Jaeger first, participant 2 = dashboard first, …
- Participants should not have seen the dataset before. Team members do not count
  as participants.

## Procedure, per episode

1. Read the task aloud: "Requests to *endpoint* became slow around *time*. Which
   service is most likely responsible?" Start the timer when the participant touches
   the mouse.
2. Stop the timer when they name a service. Record the service and the time.
3. Ask one follow-up, not timed: "What did you look at to decide?" Note it in one
   line; this is the explanation-validity check.
4. No hints. If they give up, record `gave_up` with the time and no service.

## What to record

One row per (participant, episode) in `timing_log.csv`:

| column | meaning |
|---|---|
| participant | P1, P2, … |
| tool | `jaeger` or `dashboard` |
| episode | A–D |
| order | 1–4, the position of this episode in the session |
| seconds | time to answer |
| answer | service named, or `gave_up` |
| correct | 1 if the answer matches the ground truth, else 0 |
| notes | what they looked at |

Then: `python -m experiments.usability.usability_stats experiments/usability/timing_log.csv`

## Reporting

- Median and p25–p75 time per tool, accuracy per tool, and the per-participant
  difference (dashboard minus Jaeger) so the comparison is paired.
- With fewer than 8 participants, report the numbers without significance claims.
