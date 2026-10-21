from __future__ import annotations

import copy
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def fixture(name="parallel-trace.json"):
    return json.loads((ROOT / "trace_schema" / "examples" / name).read_text(encoding="utf-8"))


def cloned(record, **changes):
    result = copy.deepcopy(record)
    result.update(changes)
    return result
