"""Write synthetic Sprint 5 plumbing inputs; never real experiment evidence."""
import argparse
import json
from pathlib import Path

from .demo import records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/sprint5-synthetic'))
    args = parser.parse_args()
    data = list(records())
    for span in data:
        faulty = int(span['trace_id'], 16) > 35 and span['service_name'] == 'payment'
        span['status'] = {'code': 'ERROR' if faulty else 'OK', 'message': 'synthetic failure' if faulty else None}
    args.output.mkdir(parents=True, exist_ok=True)
    for name, subset in [('baseline', data[:40]), ('evaluation', data[40:])]:
        (args.output / f'{name}.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in subset), encoding='utf-8')
    print(f'Wrote synthetic inputs to {args.output}')


if __name__ == '__main__':
    main()
