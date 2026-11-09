"""Verify a real payment-failure capture and write a root-cause comparison."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path

from reconstruction.smoke.astronomy_shop import cmd_verify
from .reliability import Policy, analyze, persist
from .reliability_cli import read_capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--application', default='astronomy-shop')
    parser.add_argument('--environment', default='local-controlled-experiment')
    args = parser.parse_args()
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_verify(args)
    manifest = json.loads((args.runtime / 'manifest.json').read_text(encoding='utf-8'))
    verification = json.loads((args.runtime / 'verification.json').read_text(encoding='utf-8'))
    healthy = set(verification['healthy_trace_ids'])
    faulty = set(verification['faulty_trace_ids'])
    records = read_capture(args.input)
    # Five is an explicit controlled-experiment override of the default ten.
    report = analyze([r for r in records if r['trace_id'] in healthy],
                     [r for r in records if r['trace_id'] in faulty],
                     application=args.application, environment=args.environment,
                     policy=Policy(min_samples=5))
    report['experiment_manifest'] = manifest
    report['verification'] = verification
    report['capture_sha256'] = hashlib.sha256(args.input.read_bytes()).hexdigest()
    candidates = [w for w in report['windows'] if w['scope'] == 'service' and w['error_anomaly']]
    candidates.sort(key=lambda w: (-w['error_rate_deviation'], -w['error_trace_count'], w['service_name']))
    top = candidates[0] if candidates else None
    tied = sorted({w['service_name'] for w in candidates if top and
                   (w['error_rate_deviation'], w['error_trace_count']) ==
                   (top['error_rate_deviation'], top['error_trace_count'])})
    report['diagnosis'] = {
        'method': 'error-rate-deviation-v1; descriptive association, not causal proof',
        'expected_service': manifest['root_cause_service'],
        'top_ranked_services': tied,
        'injected_service_among_top': manifest['root_cause_service'] in tied,
        'unavailable_reason': None if top else 'No service window has enough known statuses and an error-rate anomaly.',
        'rankings': candidates,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    persist(args.output / 'baselines.sqlite', report)
    (args.output / 'reliability.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    lines = ['# Controlled payment-failure evidence', '',
             'Application capture; provenance requires review. Metrics are derived from measured intervals and explicit statuses.', '',
             f"Run: `{manifest['run_id']}`. Capture SHA-256: `{report['capture_sha256']}`.", '',
             f"Healthy traces: {len(healthy)}. Fault-window traces: {len(faulty)}.", '',
             f"Injected service: **{manifest['root_cause_service']}**. Top-ranked services (ties retained): {', '.join(tied) or 'unavailable'}.", '',
             'Error propagation can produce ties. Ranking is descriptive association, not proof of causality.', '']
    if not top:
        lines += [report['diagnosis']['unavailable_reason'], '']
    for row in candidates:
        lines += [f"- {row['service_name']}: error rate {row['error_rate']:.1%}, baseline {row['baseline_error_rate']:.1%}, deviation {row['error_rate_deviation']:.1%}.",
                  '  Supporting trace IDs: ' + ', '.join(e['trace_id'] for e in row['error_evidence'])]
    (args.output / 'diagnosis.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'Wrote verified experiment report to {args.output}')


if __name__ == '__main__':
    main()
