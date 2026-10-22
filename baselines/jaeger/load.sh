#!/usr/bin/env bash
# Load a dataset into Jaeger so a person can diagnose it by hand.
#   ./load.sh path/to/dataset.csv [speed]
# Uses the replayer from ingest/, rebased to the current time so Jaeger's time
# picker finds the traces. Default speed is max.
set -euo pipefail
FILE="${1:?usage: load.sh <dataset file or RCAEval case folder> [speed]}"
SPEED="${2:-max}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../../ingest"
python3 -m traceingest replay "$FILE" --speed "$SPEED" --url http://localhost:4318 --rebase-to-now
echo "done. open http://localhost:16686 and pick the service to start from."
