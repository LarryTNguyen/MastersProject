"""Make fake cases in RCAEval's layout so the evaluator can be tested without the
real download. One service gets slow (or starts failing) after inject_time.

    python -m experiments.tests.make_cases out_dir
"""
from __future__ import annotations

import csv
import hashlib
import os
import random
import sys

SERVICES = {
    "frontend": ["checkoutservice", "productcatalogservice", "cartservice"],
    "checkoutservice": ["paymentservice", "shippingservice", "cartservice"],
    "productcatalogservice": [], "cartservice": [], "paymentservice": [], "shippingservice": [],
}
BASE_MS = {"frontend": 5, "checkoutservice": 4, "productcatalogservice": 3, "cartservice": 2,
           "paymentservice": 8, "shippingservice": 4}


def _id(*parts, n=16):
    return hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=n // 2).hexdigest()


def make_case(out_dir: str, benchmark: str, culprit: str, fault: str, instance: int,
              traces: int = 120, inject_at: int = 1_712_000_000, seed: int = 0) -> str:
    rnd = random.Random(f"{benchmark}{culprit}{fault}{instance}{seed}")
    folder = os.path.join(out_dir, f"{benchmark}_{culprit}_{fault}_{instance}")
    os.makedirs(folder, exist_ok=True)
    rows = []
    start_s = inject_at - 300
    for i in range(traces):
        t_us = (start_s + i * 600 / traces) * 1_000_000
        anomalous = t_us >= inject_at * 1_000_000
        tid = _id(folder, i, n=32)

        def emit(svc, parent, t, depth=0):
            ms = max(0.5, rnd.gauss(BASE_MS[svc], BASE_MS[svc] / 4))
            if anomalous and svc == culprit and fault == "delay":
                ms += rnd.gauss(150, 20)
            sid = _id(tid, svc, depth, len(rows))
            end = t
            for c in SERVICES[svc]:
                end = max(end, emit(c, sid, end + rnd.uniform(100, 500), depth + 1))
            end += ms * 1000
            code = 0
            if anomalous and svc == culprit and fault == "error" and rnd.random() < 0.6:
                code = 14  # gRPC UNAVAILABLE
                end += rnd.uniform(0, 2000)
            rows.append({"traceID": tid, "spanID": sid, "parentSpanID": parent or "", "serviceName": svc,
                         "operationName": f"{svc}/op", "methodName": "", "startTime": int(t),
                         "duration": int(end - t), "statusCode": code})
            return end

        emit("frontend", None, t_us)
    with open(os.path.join(folder, "traces.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(folder, "inject_time.txt"), "w") as f:
        f.write(f"{inject_at}\n")
    return folder


def make_suite(out_dir: str) -> list:
    made = []
    for i, (svc, fault) in enumerate([("paymentservice", "delay"), ("cartservice", "delay"),
                                      ("shippingservice", "delay"), ("productcatalogservice", "delay"),
                                      ("paymentservice", "error")]):
        made.append(make_case(out_dir, "re2ob", svc, fault, 1, seed=i))
    return made


if __name__ == "__main__":
    for p in make_suite(sys.argv[1] if len(sys.argv) > 1 else "data/rcaeval-synthetic"):
        print(p)
