"""Run one participant's session: load each episode, time it, write timing_log.csv.

    python -m experiments.usability.session P3            # loads episodes itself
    python -m experiments.usability.session P3 --no-load  # you load them by hand

For each of the participant's four episodes (from schedule.csv) it tells you which
tool to open, loads the episode (Jaeger has to be restarted before each Jaeger
episode; the script waits for you), prints the sentence to read aloud, and then:

  Enter   when the participant touches the mouse      (timer starts)
  type    the service they name, or gave_up, + Enter  (timer stops)
  type    one line on what they looked at, + Enter

The answer is checked against answer_key.json, so the facilitator never needs to
look at it. Names are compared loosely: "route", "ts-route-service" and
"Route Service" all match ts-route-service.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from typing import Callable, List

from . import episodes as eps
from . import schedule as sched

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FIELDS = ["participant", "tool", "episode", "order", "seconds", "answer", "correct", "notes"]


def norm(name: str) -> str:
    """'ts-route-service', 'Route Service' and 'route' all become 'route'."""
    s = name.strip().lower()
    s = re.sub(r"^ts[-_ ]", "", s)                 # Train Ticket's prefix
    s = re.sub(r"[^a-z0-9]", "", s)
    return s[:-len("service")] if s.endswith("service") and len(s) > len("service") else s


def is_correct(answer: str, culprit: str) -> bool:
    return bool(answer.strip()) and answer.strip() != "gave_up" and norm(answer) == norm(culprit)


def append_log(path: str, row: dict) -> None:
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS, lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow(row)


def run_session(participant: str, plan: List[dict], key: dict, log_path: str,
                ask: Callable[[str], str] = input, say: Callable[[str], None] = print,
                clock: Callable[[], float] = time.monotonic,
                loader: Callable[[str, str], dict] = None) -> List[dict]:
    done = []
    if not plan:
        raise SystemExit(f"{participant} is not in the schedule")
    for step in plan:
        ep, tool, order = step["episode"], step["tool"], int(step["order"])
        say(f"\n=== {participant}, episode {order} of {len(plan)}: episode {ep} in {tool.upper()} ===")
        if loader:
            if tool == "jaeger":
                ask("Restart Jaeger (it keeps data in memory), then press Enter to load. ")
            r = loader(ep, tool)
            sentence = r["read_aloud"]
        else:
            ask(f"Load episode {ep} into {tool} (episodes load {ep} --tool {tool}), then press Enter. ")
            sentence = eps.task_sentence(key[ep], "<the time load printed>")
        say(f"Open {'Jaeger at http://localhost:16686' if tool == 'jaeger' else 'the dashboard'} for the participant.")
        say(f"Read aloud:\n  {sentence}")
        ask("Press Enter when the participant touches the mouse. ")
        t0 = clock()
        answer = ask("Service they named (or gave_up): ").strip() or "gave_up"
        seconds = round(clock() - t0, 1)
        notes = ask("What did they look at? (one line): ").strip()
        row = {"participant": participant, "tool": tool, "episode": ep, "order": order, "seconds": seconds,
               "answer": answer, "correct": int(is_correct(answer, key[ep]["culprit"])), "notes": notes}
        append_log(log_path, row)
        done.append(row)
        say(f"recorded: {seconds} s")
    say(f"\n{participant} done. {sum(r['correct'] for r in done)} of {len(done)} correct.")
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("participant", help="P1, P2, ...")
    ap.add_argument("--schedule", default=os.path.join(HERE, "schedule.csv"))
    ap.add_argument("--dir", default=eps.DEFAULT_DIR, help="where prepare wrote the episodes")
    ap.add_argument("--log", default=os.path.join(HERE, "timing_log.csv"))
    ap.add_argument("--no-load", action="store_true", help="do not load episodes; you do it by hand")
    ap.add_argument("--dashboard-data", help="the dashboard's DATA_DIR (default <dir>/live_dashboard)")
    a = ap.parse_args(argv)
    with open(os.path.join(a.dir, "answer_key.json")) as f:
        key = json.load(f)
    plan = sched.for_participant(sched.read(a.schedule), a.participant)
    loader = None if a.no_load else (
        lambda ep, tool: eps.load(ep, a.dir, tool=tool, dashboard_dir=a.dashboard_data))
    run_session(a.participant, plan, key, a.log, loader=loader)


if __name__ == "__main__":
    sys.exit(main())
