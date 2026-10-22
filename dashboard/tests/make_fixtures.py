"""Write the small hand-made fixture set used by the tests.

Three traces: one fast, one slow with an error, one incomplete (its parent span
never arrived), plus two ranking windows. Run after an intended change:
    python tests/make_fixtures.py
"""
import json
import os

FX = os.path.join(os.path.dirname(__file__), "fixtures")
T0 = 1_700_000_000_000_000


def sp(t, s, p, svc, op, start, dur, status="ok", src="otlp", attrs=None):
    return {"trace_id": t, "span_id": s, "parent_span_id": p, "service": svc, "operation": op,
            "start_us": start, "duration_us": dur, "status": status, "kind": "server",
            "peer_service": None, "source": src, "attributes": attrs or {}, "v": 1}


SPANS = [
    sp("t-fast", "a1", None, "frontend", "GET /cart", T0, 20_000),
    sp("t-fast", "a2", "a1", "cartservice", "GetCart", T0 + 2_000, 10_000),
    sp("t-fast", "a3", "a2", "redis", "HGETALL", T0 + 3_000, 4_000),
    sp("t-slow", "b1", None, "frontend", "POST /checkout", T0 + 1_000_000, 300_000),
    sp("t-slow", "b2", "b1", "checkoutservice", "PlaceOrder", T0 + 1_002_000, 290_000),
    sp("t-slow", "b3", "b2", "paymentservice", "Charge", T0 + 1_005_000, 280_000, "error"),
    sp("t-slow", "b4", "b2", "cartservice", "GetCart", T0 + 1_004_000, 5_000),
    sp("t-inc", "c1", "missing", "cartservice", "GetCart", T0 + 2_000_000, None, "unset", "alibaba", {"alibaba.rpcid": "0.1"}),
    sp("t-inc", "c2", "c1", "redis", "HGETALL", T0 + 2_001_000, 3_000, "unset", "alibaba", {"alibaba.rpcid": "0.1.1"}),
]
RANKINGS = [
    {"window_start_us": T0, "window_end_us": T0 + 3_000_000, "endpoint": "POST /checkout",
     "method": "bottleneck_score", "note": None,
     "rankings": [{"rank": 1, "service": "paymentservice", "score": 0.9, "evidence": {"critical_path_ms_slow": 280.0}},
                  {"rank": 2, "service": "checkoutservice", "score": 0.1, "evidence": {}}]},
    {"window_start_us": T0 - 3_000_000, "window_end_us": T0, "endpoint": None, "method": "bottleneck_score",
     "note": "no service clearly explains a slowdown", "rankings": []},
]

if __name__ == "__main__":
    os.makedirs(FX, exist_ok=True)
    with open(os.path.join(FX, "spans.jsonl"), "w") as f:
        for s in SPANS:
            f.write(json.dumps(s, sort_keys=True) + "\n")
    with open(os.path.join(FX, "rankings.json"), "w") as f:
        json.dump(RANKINGS, f, indent=1)
    print("fixtures written to", FX)
