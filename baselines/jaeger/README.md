# Jaeger baseline (manual diagnosis)

Jaeger is the "what engineers do today" baseline in our evaluation: the same traces
go into Jaeger and into our dashboard, and we time how long a person takes to name
the bottleneck service with each.

## Setup

```bash
cd baselines/jaeger
docker compose up -d                 # UI on http://localhost:16686
./load.sh ../../data/alibaba_hour.csv      # or an RCAEval case folder
```

`load.sh` replays the dataset through the normal replayer at max speed, rebased to
the current time, into Jaeger's OTLP endpoint. Nothing about the data changes, so
Jaeger and our dashboard see exactly the same spans.

To load the same data into our dashboard for a side-by-side session:

```bash
cd ../../ingest && python -m traceingest convert ../data/alibaba_hour.csv -o ../dashboard/data/alibaba.jsonl --to jsonl
cd ../dashboard && DATA_DIR=data uvicorn app.main:app
```

## How the comparison is run

The protocol, the timing sheet and the script that summarizes it are in
`experiments/usability/`. In short: each participant gets a slow episode, has to
name the service responsible, and we record time-to-answer and whether it matched
the ground truth, once in Jaeger and once in the dashboard, with the order
alternated between participants.

## Notes

- Jaeger stores data in a Docker volume (`jaeger-data`); `docker compose down -v`
  clears it between sessions.
- Jaeger's search needs a service name; for Alibaba data the services are hashed
  names, so start from the entry service shown in our dashboard's trace list.
