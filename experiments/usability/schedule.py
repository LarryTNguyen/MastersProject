"""Who does which episode with which tool, in what order.

    python -m experiments.usability.schedule 8 > experiments/usability/schedule.csv

Four groups repeat every four participants:
  - tool order alternates, so half start with Jaeger and half with the dashboard
  - which pair of episodes goes to which tool swaps every two participants, so every
    episode is done with both tools equally often
  - A and B are Online Boutique, C and D are Train Ticket, and each tool gets one of
    each in every session
"""
from __future__ import annotations

import csv
import sys
from typing import List

# episode order per group, and which tool the first slot uses
GROUPS = [
    (("A", "B", "C", "D"), "jaeger"),      # Jaeger gets A, C
    (("A", "B", "C", "D"), "dashboard"),   # dashboard gets A, C
    (("B", "A", "D", "C"), "jaeger"),      # Jaeger gets B, D
    (("B", "A", "D", "C"), "dashboard"),   # dashboard gets B, D
]
OTHER = {"jaeger": "dashboard", "dashboard": "jaeger"}


def schedule(n: int) -> List[dict]:
    rows = []
    for i in range(n):
        episodes, first = GROUPS[i % len(GROUPS)]
        for order, ep in enumerate(episodes, start=1):
            tool = first if order % 2 == 1 else OTHER[first]
            rows.append({"participant": f"P{i + 1}", "order": order, "episode": ep, "tool": tool})
    return rows


def for_participant(rows: List[dict], participant: str) -> List[dict]:
    mine = [r for r in rows if r["participant"] == participant]
    return sorted(mine, key=lambda r: int(r["order"]))


def read(path: str) -> List[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    n = int(argv[0]) if argv else 8
    w = csv.DictWriter(sys.stdout, fieldnames=["participant", "order", "episode", "tool"], lineterminator="\n")
    w.writeheader()
    w.writerows(schedule(n))


if __name__ == "__main__":
    main()
