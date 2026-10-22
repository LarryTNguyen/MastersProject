"""accuracy@k and MRR, the way RCAEval defines them."""
from __future__ import annotations

import re
from typing import Dict, List, Optional


def canon(name: str) -> str:
    """'checkout-service', 'CheckoutService', 'checkoutservice' all compare equal."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def rank_of(truth: str, ranking: List[str]) -> Optional[int]:
    """1-based position of the true service in the ranking, or None if absent."""
    t = canon(truth)
    for i, svc in enumerate(ranking, start=1):
        c = canon(svc)
        if c == t or (len(t) >= 5 and (t in c or c in t)):
            return i
    return None


def accuracy_at_k(ranks: List[Optional[int]], k: int) -> float:
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks) if ranks else 0.0


def mrr(ranks: List[Optional[int]]) -> float:
    return sum(1.0 / r for r in ranks if r is not None) / len(ranks) if ranks else 0.0


def summarize(ranks: List[Optional[int]]) -> Dict[str, float]:
    return {"cases": len(ranks), "acc@1": accuracy_at_k(ranks, 1), "acc@3": accuracy_at_k(ranks, 3),
            "acc@5": accuracy_at_k(ranks, 5), "mrr": mrr(ranks),
            "not_found": sum(1 for r in ranks if r is None)}
