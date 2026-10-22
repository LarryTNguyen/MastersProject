"""One command for every number and figure in the report.

    python -m experiments.run experiments/config.json

Steps (each skipped if its input isn't configured): replay, rcaeval, freshness,
usability, cost, figures, summary.md. Everything lands in <out>/<timestamp>/
with a copy of the config.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import List

from . import _paths

_paths.setup()
from . import figures, rcaeval_eval
from .usability.usability_stats import summarize as usability_summary

ROOT = _paths.ROOT


def step_replay(cfg: dict, out: str, log) -> List[dict]:
    done = []
    for job in cfg.get("replay", []):
        cmd = [sys.executable, "-m", "traceingest", "replay", job["input"], "--speed", str(job.get("speed", "max")), "--quiet"]
        if job.get("url"):
            cmd += ["--url", job["url"]]
            if job.get("token_env") and os.environ.get(job["token_env"]):
                cmd += ["--token", os.environ[job["token_env"]]]
            if job.get("rebase_to_now", True):
                cmd.append("--rebase-to-now")
        else:
            cmd += ["--out-dir", os.path.join(out, "replay", os.path.basename(job["input"]))]
        log(f"replay: {' '.join(cmd[3:])}")
        r = subprocess.run(cmd, cwd=os.path.join(ROOT, "ingest"), capture_output=True, text=True)
        rec = {"input": job["input"], "ok": r.returncode == 0, "stats": None, "error": r.stderr.strip()[-500:] or None}
        if r.returncode == 0:
            try:
                rec["stats"] = json.loads(r.stdout)
            except ValueError:
                pass
        done.append(rec)
    if done:
        json.dump(done, open(os.path.join(out, "replay.json"), "w"), indent=2)
    return done


def step_rcaeval(cfg: dict, out: str, log) -> dict:
    rc = cfg.get("rcaeval")
    if not rc:
        return {}
    dirs = rcaeval_eval.expand(rc["cases"])
    if not dirs:
        log("rcaeval: no case folders matched, skipping")
        return {}
    methods = list(rc.get("methods", ["slowest_average", "longest_span", "critical_path_only"]))
    methods += [f"{mod}" for mod in rc.get("extra_methods", [])]
    log(f"rcaeval: {len(dirs)} cases x {len(methods)} methods")
    result = rcaeval_eval.evaluate(dirs, methods, log)
    rcaeval_eval.write_results(result, out)
    return result["summary"]


def step_freshness(cfg: dict, out: str, log) -> dict:
    fr = cfg.get("freshness")
    if not fr:
        return {}
    data = {}
    if fr.get("dashboard_url"):
        try:
            with urllib.request.urlopen(fr["dashboard_url"].rstrip("/") + "/api/pipeline?window_s=86400", timeout=10) as r:
                data = json.load(r)
            log("freshness: pulled from the dashboard")
        except Exception as e:
            log(f"freshness: dashboard not reachable ({e})")
    if fr.get("file") and os.path.exists(fr["file"]):
        data.update(json.load(open(fr["file"])))
        log(f"freshness: loaded {fr['file']}")
    if data:
        json.dump(data, open(os.path.join(out, "freshness.json"), "w"), indent=2)
    return data


def step_usability(cfg: dict, out: str, log) -> dict:
    path = cfg.get("usability_log")
    if not path or not os.path.exists(path):
        return {}
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("seconds")]
    if not rows:
        log("usability: log is empty, skipping")
        return {}
    s = usability_summary(rows)
    json.dump(s, open(os.path.join(out, "usability_summary.json"), "w"), indent=2)
    log(f"usability: {len(rows)} rows from {s['paired_n']} paired participants")
    return s


def step_cost(cfg: dict, out: str, log) -> List[dict]:
    path = cfg.get("cost_csv")
    if not path or not os.path.exists(path):
        return []
    shutil.copy(path, os.path.join(out, "cost.csv"))
    with open(path) as f:
        rows = list(csv.DictReader(f))
    log(f"cost: {len(rows)} rows")
    return rows


def write_summary(out: str, cfg: dict, rc: dict, fresh: dict, usab: dict, cost: List[dict], made: List[str]) -> str:
    lines = [f"# Experiment run {os.path.basename(out)}", "",
             f"Config: `{cfg.get('_config_path', '?')}`", ""]
    if rc:
        lines += ["## RCAEval", "", "| method | cases | acc@1 | acc@3 | MRR |", "|---|---|---|---|---|"]
        for m, s in rc.items():
            lines.append(f"| {m} | {s['cases']} | {s['acc@1']:.3f} | {s['acc@3']:.3f} | {s['mrr']:.3f} |")
        lines.append("")
    if fresh:
        st = fresh.get("staleness_ms", {})
        lines += ["## Freshness", "",
                  f"staleness p50 {st.get('p50')} ms, p95 {st.get('p95')} ms, p99 {st.get('p99')} ms "
                  f"(target: p95 under 60000 ms); incomplete traces {100 * fresh.get('incomplete_rate', 0):.1f}%", ""]
    if usab:
        lines += ["## Usability", ""]
        for tool, t in usab["tools"].items():
            lines.append(f"- {tool}: median {t['median_s']:.0f} s, accuracy {t['accuracy']:.0%} (n={t['n']})")
        if usab["paired_n"]:
            lines.append(f"- paired: {usab['faster_with_dashboard']}/{usab['paired_n']} faster with the dashboard, "
                         f"mean difference {usab['paired_mean_diff_s']:+.0f} s")
        lines.append("")
    if cost:
        lines += ["## Cost", ""] + [f"- {r['name']}: ${float(r['usd_month']):.2f}/month" for r in cost] + [""]
    if made:
        lines += ["## Figures", ""] + [f"- `{os.path.relpath(p, out)}`" for p in made] + [""]
    path = os.path.join(out, "summary.md")
    open(path, "w").write("\n".join(lines))
    return path


def run(config_path: str, out_root: str = None, log=print) -> str:
    cfg = json.load(open(config_path))
    cfg["_config_path"] = config_path
    out_root = out_root or cfg.get("out", "experiments/results")
    out = os.path.join(out_root, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)
    shutil.copy(config_path, os.path.join(out, "config.json"))
    step_replay(cfg, out, log)
    rc = step_rcaeval(cfg, out, log)
    fresh = step_freshness(cfg, out, log)
    usab = step_usability(cfg, out, log)
    cost = step_cost(cfg, out, log)
    made = figures.from_results_dir(out)
    log(f"figures: {len(made)}")
    summary = write_summary(out, cfg, rc, fresh, usab, cost, made)
    latest = os.path.join(out_root, "latest")
    if os.path.islink(latest) or os.path.exists(latest):
        try:
            os.remove(latest) if os.path.islink(latest) else shutil.rmtree(latest)
        except OSError:
            pass
    try:
        os.symlink(os.path.basename(out), latest)
    except OSError:
        shutil.copytree(out, latest)
    log(f"done: {summary}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", nargs="?", default="experiments/config.json")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    run(a.config, a.out)


if __name__ == "__main__":
    main()
