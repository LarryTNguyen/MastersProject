"""Run from the repository root: python -m diagnosis INPUT --output DIRECTORY."""
import argparse
import hashlib
import json
from pathlib import Path

from reconstruction.trace_reconstruction.__main__ import _records, _validator
from .episodes import diagnose


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--assume-synchronous', action='store_true')
    parser.add_argument('--synthetic', action='store_true', help='label output as synthetic, not a real case study')
    parser.add_argument('--window-seconds', type=int, default=300)
    parser.add_argument('--min-samples', type=int, default=10)
    parser.add_argument('--slow-ratio', type=float, default=1.5)
    parser.add_argument('--min-excess-ms', type=float, default=5.0)
    parser.add_argument('--alert-delta', type=float, default=0.25)
    args = parser.parse_args()
    files = sorted(args.input.glob('*.jsonl')) if args.input.is_dir() else [args.input]
    if not files:
        parser.error('no JSONL input files found')
    validate = _validator()

    def validated_records():
        for file in files:
            for number, record in enumerate(_records(str(file)), 1):
                errors = validate(record)
                if errors:
                    raise ValueError(f'{file}: record {number}: ' + '; '.join(errors))
                yield record

    result = diagnose(validated_records(),
                      assume_synchronous=args.assume_synchronous, window_seconds=args.window_seconds,
                      min_samples=args.min_samples, slow_ratio=args.slow_ratio,
                      min_excess_ms=args.min_excess_ms, alert_delta=args.alert_delta)
    result['input_files'] = [{'path': str(p.resolve()), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
    result['data_label'] = 'synthetic' if args.synthetic else 'user-provided; provenance requires review'
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'episodes.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    # Native dashboard JSONL-store ranking contract, alongside the detailed report.
    rankings = [{'window_start_us': int(w['window_start_ns']) // 1000,
                 'window_end_us': int(w['window_end_ns']) // 1000,
                 'endpoint': w['root_operation'], 'method': result['method'] + ' (estimate)',
                 'note': w['note'],
                 'rankings': [{'rank': r['rank'], 'service': r['service_name'], 'score': r['score'],
                               'evidence': {k: v for k, v in r.items() if k not in ('rank', 'service', 'service_name', 'score')}}
                              for r in w['rankings']]} for w in result['windows']]
    (args.output / 'rankings.json').write_text(json.dumps(rankings, indent=2), encoding='utf-8')
    lines = ['# Tail episode investigation', '', 'Data: ' + result['data_label'], '',
             'This report uses a synchronous critical-path heuristic. The consistency check is not causal proof.', '',
             'Excluded traces: `' + json.dumps(result['excluded_traces']) + '`', '']
    for window in result['windows']:
        lines += ['## ' + window['endpoint'], '',
                  f"Run `{window['run_id']}`, window start `{window['window_start_ns']}` ns; {window['trace_count']} traces.", '']
        if window['note']:
            lines += [window['note'], '']
        for row in window['rankings']:
            lines += [f"{row['rank']}. **{row['service']}** — score {row['score']:.3f}; excess {row['excess_ms']:.2f} ms; critical-path share {row['normal_share']:.1%} → {row['slow_share']:.1%}.",
                      f"   Median duration: normal {row['normal_duration_ms']:.2f} ms, slow {row['slow_duration_ms']:.2f} ms, adjusted slow {row['adjusted_slow_duration_ms']:.2f} ms.",
                      '   Supporting trace IDs: ' + ', '.join(row['supporting_trace_ids']), '']
        lines += ['Score-spike flags: `' + json.dumps(window['alerts']) + '`', '']
    (args.output / 'case-study.md').write_text('\n'.join(lines), encoding='utf-8')
    print(f"Wrote {len(result['windows'])} windows to {args.output}")


if __name__ == '__main__':
    main()
