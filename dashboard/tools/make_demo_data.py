"""Generate a small demo dataset in the common span format.

Writes data/demo_spans.jsonl (about 200 e-commerce traces over 10 minutes,
with one service that gets slow in the second half) and data/rankings.json
(one example ranking window in the shape the diagnosis step will write).

    python tools/make_demo_data.py [out_dir]
"""
import hashlib
import json
import os
import random
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "data")
random.seed(7)

# service -> (children, base_ms, spread_ms)
SHOP = {
    "frontend":            (["checkoutservice", "productcatalogservice", "cartservice"], 8, 4),
    "checkoutservice":     (["paymentservice", "shippingservice", "emailservice", "cartservice"], 6, 3),
    "productcatalogservice": ([], 4, 2),
    "cartservice":         (["redis"], 3, 1),
    "redis":               ([], 1, 1),
    "paymentservice":      ([], 12, 6),
    "shippingservice":     (["quoteservice"], 5, 2),
    "quoteservice":        ([], 7, 3),
    "emailservice":        ([], 4, 2),
}
OPS = {"frontend": "HTTP POST /cart/checkout", "checkoutservice": "PlaceOrder", "productcatalogservice": "GetProduct",
       "cartservice": "GetCart", "redis": "HGETALL", "paymentservice": "Charge", "shippingservice": "ShipOrder",
       "quoteservice": "GetQuote", "emailservice": "SendOrderConfirmation"}
START_US = 1_760_428_800_000_000   # 2025-10-14 08:00 UTC
SLOW_AFTER_US = START_US + 5 * 60_000_000


def sid(*parts):
    return hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).hexdigest()


def tid(i):
    return hashlib.blake2b(f"demo-trace-{i}".encode(), digest_size=16).hexdigest()


def build(trace_id, service, start_us, parent, depth, out, slow):
    children, base, spread = SHOP[service]
    ms = max(0.3, random.gauss(base, spread / 2))
    if slow and service == "paymentservice":
        ms += random.gauss(180, 40)          # the injected slowdown
    span_id = sid(trace_id, service, depth, len(out))
    t = start_us + int(random.uniform(0.2, 1.0) * 1000)
    end = t
    for c in children:
        c_end = build(trace_id, c, end + int(random.uniform(0.1, 0.6) * 1000), span_id, depth + 1, out, slow)
        end = max(end, c_end)
    end += int(ms * 1000)
    status = "error" if (service == "paymentservice" and slow and random.random() < 0.08) else "ok"
    out.append({"trace_id": trace_id, "span_id": span_id, "parent_span_id": parent, "service": service,
                "operation": OPS[service], "start_us": t, "duration_us": end - t, "status": status,
                "kind": "server", "peer_service": None, "source": "otlp",
                "attributes": {"demo": True}, "v": 1})
    return end


os.makedirs(OUT, exist_ok=True)
spans = []
n = 200
for i in range(n):
    start = START_US + int(i * 600_000_000 / n) + random.randint(0, 2_000_000)
    build(tid(i), "frontend", start, None, 0, spans, slow=start >= SLOW_AFTER_US)
# a couple of incomplete traces (parent never arrived) to show the "incomplete" flag
for s in spans[-6:-3]:
    if s["parent_span_id"]:
        s["parent_span_id"] = "deadbeefdeadbeef"
spans.sort(key=lambda s: (s["trace_id"], s["start_us"]))
with open(os.path.join(OUT, "demo_spans.jsonl"), "w") as f:
    for s in spans:
        f.write(json.dumps(s, sort_keys=True) + "\n")

rankings = [{
    "window_start_us": SLOW_AFTER_US, "window_end_us": START_US + 600_000_000,
    "endpoint": "HTTP POST /cart/checkout", "method": "bottleneck_score",
    "note": None,
    "rankings": [
        {"rank": 1, "service": "paymentservice", "score": 0.91,
         "evidence": {"critical_path_ms_slow": 186.0, "critical_path_ms_normal": 12.1, "self_time_ratio": 0.78, "error_rate": 0.08}},
        {"rank": 2, "service": "checkoutservice", "score": 0.22,
         "evidence": {"critical_path_ms_slow": 9.4, "critical_path_ms_normal": 6.2, "self_time_ratio": 0.05, "error_rate": 0.0}},
        {"rank": 3, "service": "shippingservice", "score": 0.08,
         "evidence": {"critical_path_ms_slow": 5.1, "critical_path_ms_normal": 5.0, "self_time_ratio": 0.03, "error_rate": 0.0}},
    ],
}, {
    "window_start_us": START_US, "window_end_us": SLOW_AFTER_US,
    "endpoint": "HTTP POST /cart/checkout", "method": "bottleneck_score",
    "note": "no service clearly explains a slowdown in this window",
    "rankings": [
        {"rank": 1, "service": "paymentservice", "score": 0.31, "evidence": {"critical_path_ms_slow": 14.0, "critical_path_ms_normal": 12.1}},
        {"rank": 2, "service": "frontend", "score": 0.27, "evidence": {"critical_path_ms_slow": 9.0, "critical_path_ms_normal": 8.2}},
    ],
}]
with open(os.path.join(OUT, "rankings.json"), "w") as f:
    json.dump(rankings, f, indent=1)
print(f"wrote {len(spans)} spans in {n} traces to {os.path.abspath(OUT)}")
