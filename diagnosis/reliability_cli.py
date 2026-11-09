"""Compare canonical captures: python -m diagnosis.reliability_cli --help."""
import argparse
import hashlib
import json
from pathlib import Path

from .reliability import Policy, analyze, persist


def read_capture(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--application', required=True)
    parser.add_argument('--environment', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--synthetic', action='store_true')
    parser.add_argument('--min-samples', type=int, default=10)
    parser.add_argument('--window-seconds', type=int, default=300)
    parser.add_argument('--slow-ratio', type=float, default=1.5)
    parser.add_argument('--error-delta', type=float, default=.1)
    args = parser.parse_args()
    report = analyze(read_capture(args.baseline), read_capture(args.input),
                     application=args.application, environment=args.environment, synthetic=args.synthetic,
                     policy=Policy(min_samples=args.min_samples, window_seconds=args.window_seconds,
                                   slow_ratio=args.slow_ratio, error_delta=args.error_delta))
    report['input_files'] = [{'role': role, 'path': str(path.resolve()),
                              'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                             for role, path in [('baseline', args.baseline), ('evaluation', args.input)]]
    args.output.mkdir(parents=True, exist_ok=True)
    report_id = persist(args.output / 'baselines.sqlite', report)
    (args.output / 'reliability.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f'Persisted report {report_id} with {len(report["baselines"])} baselines and {len(report["windows"])} windows')


if __name__ == '__main__':
    main()
