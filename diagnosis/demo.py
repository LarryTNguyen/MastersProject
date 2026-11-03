"""Create synthetic canonical timing data for local plumbing checks only."""
import copy
import json
import sys
from pathlib import Path


def records():
    fixture = json.loads((Path(__file__).resolve().parents[1] / 'trace_schema/examples/parallel-trace.json').read_text())
    for i in range(40):
        slow = i >= 35
        start = 1791936000000000000 + i * 1_000_000_000
        for index, template in enumerate(fixture[:2]):
            span = copy.deepcopy(template)
            span['trace_id'] = f'{i + 1:032x}'
            a = start + (10_000_000 if index else 0)
            duration = ((260 if slow else 60) if index else (300 if slow else 100)) * 1_000_000
            span.update(start_time_ns=str(a), end_time_ns=str(a + duration), duration_ns=str(duration),
                        received_at_unix_ns=str(start + 400_000_000))
            span['quality_flags'] = ['synthetic_demo']
            yield span


if __name__ == '__main__':
    path = Path(sys.argv[1] if len(sys.argv) > 1 else 'data/sprint4/spans.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in records()), encoding='utf-8')
    print(f'Wrote synthetic traces to {path}')
