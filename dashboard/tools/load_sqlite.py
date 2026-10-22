"""Load normalized span files (and rankings.json) into a SQLite database.

    python tools/load_sqlite.py data/ traces.db
    STORE=sqlite SQLITE_PATH=traces.db uvicorn app.main:app --reload
"""
import glob
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.sql_store import SqlStore  # noqa: E402

data_dir, db_path = sys.argv[1], sys.argv[2]
store = SqlStore(sqlite3.connect(db_path))
store.create_schema()
spans = []
for f in sorted(glob.glob(os.path.join(data_dir, "**", "*.jsonl"), recursive=True)):
    with open(f) as fh:
        spans.extend(json.loads(l) for l in fh if l.strip())
n = store.load_spans(spans)
rk = os.path.join(data_dir, "rankings.json")
m = store.load_rankings(json.load(open(rk))) if os.path.exists(rk) else 0
print(f"loaded {n} spans and {m} ranking rows into {db_path}")
