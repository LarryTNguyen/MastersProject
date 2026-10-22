"""Make the sibling packages importable (ingest/ for readers, dashboard/ for analysis)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setup():
    for sub in ("ingest", "dashboard"):
        p = os.path.join(ROOT, sub)
        if p not in sys.path:
            sys.path.insert(0, p)
