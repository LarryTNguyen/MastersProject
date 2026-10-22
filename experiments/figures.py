"""Figures for the report, drawn from the runner's saved outputs."""
from __future__ import annotations

import csv
import json
import os
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

COLORS = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2", "#edc948", "#b07aa1"]


def _save(fig, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def rcaeval_accuracy(summary: Dict[str, dict], path: str) -> str:
    """Grouped bars: acc@1, acc@3, MRR per method."""
    methods = list(summary)
    metrics = ["acc@1", "acc@3", "mrr"]
    fig, ax = plt.subplots(figsize=(7, 3.6))
    width = 0.8 / max(1, len(methods))
    for i, m in enumerate(methods):
        vals = [summary[m][k] for k in metrics]
        xs = [j + i * width for j in range(len(metrics))]
        bars = ax.bar(xs, vals, width, label=m.replace("_", " "), color=COLORS[i % len(COLORS)])
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.01, f"{v:.2f}", ha="center", va="bottom", fontsize=8)
    ax.set_xticks([j + width * (len(methods) - 1) / 2 for j in range(len(metrics))])
    ax.set_xticklabels(["accuracy@1", "accuracy@3", "MRR"])
    ax.set_ylim(0, 1.08)
    ax.set_ylabel("score")
    n = next(iter(summary.values()))["cases"] if summary else 0
    ax.set_title(f"RCAEval root-cause ranking ({n} cases)")
    ax.legend(frameon=False, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=min(4, len(methods)))
    ax.spines[["top", "right"]].set_visible(False)
    return _save(fig, path)


def rcaeval_by_fault(summary: Dict[str, dict], path: str) -> str:
    """acc@1 per fault type, one line per method."""
    faults = sorted({f for s in summary.values() for f in s.get("by_fault", {})})
    if not faults:
        return ""
    fig, ax = plt.subplots(figsize=(7, 3.4))
    for i, (m, s) in enumerate(summary.items()):
        ax.plot(faults, [s["by_fault"].get(f, {}).get("acc@1", 0) for f in faults], marker="o",
                label=m.replace("_", " "), color=COLORS[i % len(COLORS)])
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("accuracy@1")
    ax.set_title("accuracy@1 by fault type")
    ax.legend(frameon=False, fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    return _save(fig, path)


def freshness_cdf(staleness_ms: List[float], path: str, target_ms: float = 60_000) -> str:
    """CDF of staleness with the 60 s target line (N1)."""
    xs = sorted(staleness_ms)
    ys = [(i + 1) / len(xs) for i in range(len(xs))]
    fig, ax = plt.subplots(figsize=(6, 3.4))
    ax.plot([x / 1000 for x in xs], ys, color=COLORS[0])
    ax.axvline(target_ms / 1000, color=COLORS[3], linestyle="--", label=f"target {target_ms / 1000:.0f} s")
    ax.axhline(0.95, color="#999", linestyle=":", linewidth=1)
    ax.set_xlabel("staleness (s): span end to visible on dashboard")
    ax.set_ylabel("fraction of spans")
    ax.set_title("Freshness")
    ax.legend(frameon=False, fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    return _save(fig, path)


def usability(stats: Dict[str, dict], path: str) -> str:
    """Time to diagnosis per tool (median with p25–p75 bars) and accuracy."""
    tools = list(stats)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(7.5, 3.4))
    for i, t in enumerate(tools):
        s = stats[t]
        a1.bar(i, s["median_s"], color=COLORS[i % len(COLORS)],
               yerr=[[s["median_s"] - s["p25_s"]], [s["p75_s"] - s["median_s"]]], capsize=4)
        a2.bar(i, s["accuracy"], color=COLORS[i % len(COLORS)])
        a2.text(i, s["accuracy"] + 0.02, f"{s['accuracy']:.0%}", ha="center", fontsize=8)
    for ax, title, ylabel in ((a1, "time to name the bottleneck", "seconds (median, p25–p75)"),
                              (a2, "correct diagnoses", "fraction")):
        ax.set_xticks(range(len(tools)))
        ax.set_xticklabels(tools)
        ax.set_title(title, fontsize=10)
        ax.set_ylabel(ylabel)
        ax.spines[["top", "right"]].set_visible(False)
    a2.set_ylim(0, 1.1)
    return _save(fig, path)


def cost_table(rows: List[dict], path: str) -> str:
    """Monthly cost comparison as a simple horizontal bar chart. rows: [{name, usd_month}]."""
    fig, ax = plt.subplots(figsize=(6, 0.6 + 0.5 * len(rows)))
    names = [r["name"] for r in rows]
    vals = [r["usd_month"] for r in rows]
    ax.barh(names, vals, color=[COLORS[i % len(COLORS)] for i in range(len(rows))])
    for i, v in enumerate(vals):
        ax.text(v, i, f"  ${v:,.2f}", va="center", fontsize=8)
    ax.set_xlabel("USD per month")
    ax.set_title("Measured monthly cost")
    ax.spines[["top", "right"]].set_visible(False)
    ax.invert_yaxis()
    return _save(fig, path)


def from_results_dir(out_dir: str) -> List[str]:
    """Draw every figure the saved results allow."""
    made = []
    fig_dir = os.path.join(out_dir, "figures")
    sp = os.path.join(out_dir, "rcaeval_summary.json")
    if os.path.exists(sp):
        summary = json.load(open(sp))["summary"]
        if summary:
            made.append(rcaeval_accuracy(summary, os.path.join(fig_dir, "rcaeval_accuracy.png")))
            p = rcaeval_by_fault(summary, os.path.join(fig_dir, "rcaeval_by_fault.png"))
            if p:
                made.append(p)
    fp = os.path.join(out_dir, "freshness.json")
    if os.path.exists(fp):
        data = json.load(open(fp))
        vals: Optional[List[float]] = data.get("staleness_ms_values")
        if vals:
            made.append(freshness_cdf(vals, os.path.join(fig_dir, "freshness_cdf.png")))
    up = os.path.join(out_dir, "usability_summary.json")
    if os.path.exists(up):
        made.append(usability(json.load(open(up))["tools"], os.path.join(fig_dir, "usability.png")))
    cp = os.path.join(out_dir, "cost.csv")
    if os.path.exists(cp):
        with open(cp) as f:
            rows = [{"name": r["name"], "usd_month": float(r["usd_month"])} for r in csv.DictReader(f)]
        if rows:
            made.append(cost_table(rows, os.path.join(fig_dir, "cost.png")))
    return made
