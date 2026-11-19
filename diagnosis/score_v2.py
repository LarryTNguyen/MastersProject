"""Reference-based bounded evidence score; descriptive association, not causality."""
from collections import defaultdict
from dataclasses import asdict, dataclass
import math
import json
from statistics import median

from .reliability import observations, digest
from reconstruction.trace_reconstruction.topology import group_records

ALGORITHM = 'bottleneck-score-v2'
DEFAULT_WEIGHTS = {'critical_path': .30, 'latency': .30, 'prevalence': .15, 'error': .25}


@dataclass(frozen=True)
class Configuration:
    window_seconds: int = 300
    min_samples: int = 10
    slow_ratio: float = 1.5
    floor_ms: float = 1.0
    timing_policy: str = 'measured'
    assume_synchronous: bool = False

    def __post_init__(self):
        if (self.window_seconds <= 0 or self.min_samples < 2 or
                not math.isfinite(self.slow_ratio) or self.slow_ratio <= 1 or
                not math.isfinite(self.floor_ms) or self.floor_ms <= 0 or
                self.timing_policy not in ('measured', 'measured-or-derived')):
            raise ValueError('invalid score configuration')


def bounded_excess(value, baseline, floor):
    """Positive relative increase x/(1+x), bounded [0,1)."""
    excess = max(0, value - baseline)
    return excess / (max(baseline, floor) + excess)


def combine(signals, weights):
    available = {k: w for k, w in weights.items()
                 if w > 0 and signals[k]['normalized'] is not None}
    denominator = sum(available.values())
    effective = {k: available.get(k, 0) / denominator if denominator else 0 for k in weights}
    score = sum(effective[k] * signals[k]['normalized'] for k in available) if denominator else None
    return score, effective


def _key(row):
    return (row['run_id'], tuple(row['endpoint']), row['service_namespace'], row['service_name'])


def _cp(records, synchronous):
    values = {}
    if not synchronous:
        return values
    from trace_schema.validate import validate_record
    for group in group_records(r for r in records if not validate_record(r)).values():
        trace = group.reconstruct(assume_synchronous=synchronous)
        if trace.critical_path is None:
            continue
        times = defaultdict(int)
        for span in trace.spans:
            times[(span['service_namespace'], span['service_name'])] += int(
                trace.timing_features[span['span_id']]['critical_path_contribution_ns'])
        for (namespace, service), duration in times.items():
            values[(trace.run_id, trace.trace_id, namespace, service)] = duration
    return values


def _evidence(rows):
    return [{'trace_id': r['trace_id'], 'span_ids': r['span_ids'],
             'error_span_ids': r['error_span_ids'], 'timing_qualities': r['timing_qualities']} for r in rows]


def _signal(raw, normalized, baseline, base, target, reason=None):
    return {'raw': raw, 'normalized': normalized, 'baseline': baseline,
            'baseline_sample_count': len(base), 'sample_count': len(target),
            'availability': 'available' if normalized is not None else 'unavailable',
            'reason': reason, 'baseline_evidence': _evidence(base), 'evidence': _evidence(target)}


def diagnose(baseline_records, records, *, weights=None, **settings):
    config = Configuration(**settings)
    weights = dict(DEFAULT_WEIGHTS if weights is None else weights)
    if (set(weights) != set(DEFAULT_WEIGHTS) or
            any(not math.isfinite(w) or w < 0 for w in weights.values()) or not sum(weights.values())):
        raise ValueError('weights must specify all four signals, nonnegative and with positive sum')
    baseline_records, records = list(baseline_records), list(records)
    # Overlap must be rejected even when a malformed trace would later be excluded.
    ids = lambda rr: {(r.get('run_id'), r.get('trace_id')) for r in rr if isinstance(r, dict)}
    if ids(baseline_records) & ids(records):
        raise ValueError('baseline and evaluation must contain disjoint trace IDs')
    options = dict(timing_policy=config.timing_policy, allow_uncontained=True)
    base, bex, binvalid, bconflicts = observations(baseline_records, **options)
    target, excluded, invalid, conflicts = observations(records, **options)
    base = [r for r in base if r['scope'] == 'service']
    target = [r for r in target if r['scope'] == 'service']
    lookup, buckets = defaultdict(list), defaultdict(list)
    for row in base:
        lookup[_key(row)].append(row)
    width = config.window_seconds * 1_000_000_000
    for row in target:
        buckets[(row['run_id'], tuple(row['endpoint']), int(row['start_ns']) // width * width)].append(row)
    cp_base = _cp(baseline_records, config.assume_synchronous)
    cp_target = _cp(records, config.assume_synchronous)
    cpkey = lambda r: (r['run_id'], r['trace_id'], r['service_namespace'], r['service_name'])
    windows = []
    for (run, endpoint, start), rows in sorted(buckets.items()):
        services = defaultdict(list)
        for row in rows:
            services[_key(row)].append(row)
        ranking = []
        for key, current in sorted(services.items(), key=lambda item: str(item[0])):
            reference = lookup[key]
            signals = {}
            for name in DEFAULT_WEIGHTS:
                signals[name] = _signal(None, None, None, reference, current, 'no_matching_reference')
            if reference:
                b = median(r['duration_ns'] for r in reference)
                t = median(r['duration_ns'] for r in current)
                floor = config.floor_ms * 1e6
                signals['latency'] = _signal(t - b, bounded_excess(t, b, floor), b, reference, current)
                threshold = b * config.slow_ratio
                bp = sum(r['duration_ns'] >= threshold for r in reference) / len(reference)
                tp = sum(r['duration_ns'] >= threshold for r in current) / len(current)
                signals['prevalence'] = _signal(tp - bp, max(0, tp - bp), bp, reference, current)
                signals['prevalence'].update(threshold_ns=threshold, current_prevalence=tp)
                bk = [r for r in reference if r['error'] is not None]
                tk = [r for r in current if r['error'] is not None]
                br = sum(r['error'] for r in bk) / len(bk) if bk else None
                tr = sum(r['error'] for r in tk) / len(tk) if tk else None
                delta = tr - br if br is not None and tr is not None else None
                signals['error'] = _signal(delta, max(0, delta) if delta is not None else None,
                                           br, bk, tk, 'insufficient_known_status' if delta is None else None)
                signals['error']['current_rate'] = tr
                cb = [r for r in reference if cpkey(r) in cp_base]
                ct = [r for r in current if cpkey(r) in cp_target]
                bc = median(cp_base[cpkey(r)] for r in cb) if cb else None
                tc = median(cp_target[cpkey(r)] for r in ct) if ct else None
                signals['critical_path'] = _signal(tc - bc if tc is not None and bc is not None else None,
                    bounded_excess(tc, bc, floor) if tc is not None and bc is not None else None,
                    bc, cb, ct, None if cb and ct else 'requires_synchronous_contained_intervals')
            score, effective = combine(signals, weights)
            support = sum(effective[k] * min(1, signals[k]['sample_count'] / config.min_samples,
                          signals[k]['baseline_sample_count'] / config.min_samples) for k in weights)
            quality = sum((.75 if 'DERIVED' in r['timing_qualities'] else 1) *
                          (1 if r['contained'] else .5) for r in reference + current) / len(reference + current)
            affected = [r for r in excluded if r['run_id'] == run and r['endpoint'] == list(endpoint)
                        and r['start_ns'] is not None and start <= int(r['start_ns']) < start + width]
            affected_base = [r for r in bex if r['run_id'] == run and r['endpoint'] == list(endpoint)]
            eligible = len({r['trace_id'] for r in rows})
            eligible_base = len({r['trace_id'] for r in base if r['run_id'] == run and tuple(r['endpoint']) == endpoint})
            retention = min(eligible / (eligible + len(affected)),
                            eligible_base / max(1, eligible_base + len(affected_base)))
            quality *= retention
            coverage = sum(weights[k] for k in weights if effective[k] > 0) / sum(weights.values())
            confidence = support * quality * coverage
            ranking.append({'service': '/'.join(filter(None, key[2:])), 'service_name': key[3],
                            'score': score, 'signals': signals, 'effective_weights': effective,
                            'confidence': {'value': confidence, 'sample_support': support, 'trace_quality': quality,
                                           'signal_coverage': coverage, 'eligible_fraction': retention, 'label': 'low' if confidence < .8 else 'supported',
                                           'is_probability': False}})
        ranking.sort(key=lambda r: (r['score'] is None, -(r['score'] or 0), r['service']))
        for i, row in enumerate(ranking, 1):
            row['rank'] = i
        windows.append({'run_id': run, 'endpoint': list(endpoint), 'root_operation': endpoint[2],
                        'window_start_ns': str(start), 'window_end_ns': str(start + width),
                        'trace_count': len({r['trace_id'] for r in rows}), 'rankings': ranking,
                        'note': 'Candidates are associations; equal scores remain tied in evidence.'})
    configuration = {**asdict(config), 'weights': weights}
    return {'method': ALGORITHM, 'configuration': configuration, 'configuration_id': digest(configuration),
            'input_hash': digest(sorted(records, key=lambda r: json.dumps(r, sort_keys=True))),
            'baseline_hash': digest(sorted(baseline_records, key=lambda r: json.dumps(r, sort_keys=True))),
            'timing_qualities': sorted({r.get('timing_quality', 'UNKNOWN') for r in records if isinstance(r, dict)}),
            'data_label': 'synthetic' if any(isinstance(r, dict) and any(
                'synthetic' in str(flag).lower() for flag in r.get('quality_flags', []))
                for r in baseline_records + records) else 'user-provided; provenance requires review',
            'assumptions': ['Operator-selected reference must be healthy and share run/endpoint identity.',
                            'Critical path is a synchronous estimate; caller gaps are not reassigned to callees.',
                            'Uncontained traces support inclusive latency/status only, never critical path.',
                            'Errors may propagate to callers. Missing services cannot be diagnosed.',
                            'Confidence is a support heuristic, not a calibrated probability.'],
            'windows': windows, 'excluded_traces': excluded, 'baseline_excluded_traces': bex,
            'invalid_records': invalid, 'baseline_invalid_records': binvalid,
            'conflicts': conflicts, 'baseline_conflicts': bconflicts}


def main():
    import argparse
    import json
    from pathlib import Path
    from .reliability_cli import read_capture
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timing-policy', choices=['measured', 'measured-or-derived'], default='measured')
    parser.add_argument('--assume-synchronous', action='store_true')
    parser.add_argument('--min-samples', type=int, default=10)
    parser.add_argument('--weights', type=json.loads, help='JSON object with all four signal weights')
    args = parser.parse_args()
    result = diagnose(read_capture(args.baseline), read_capture(args.input), weights=args.weights,
                      timing_policy=args.timing_policy, min_samples=args.min_samples,
                      assume_synchronous=args.assume_synchronous)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'score-v2.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    rankings = [{'window_start_us': int(w['window_start_ns']) // 1000,
                 'window_end_us': int(w['window_end_ns']) // 1000,
                 'endpoint': w['root_operation'], 'method': result['method'], 'note': w['note'],
                 'rankings': [{'rank': r['rank'], 'service': r['service_name'], 'score': r['score'],
                               'evidence': r} for r in w['rankings'] if r['score'] is not None]}
                for w in result['windows']]
    (args.output / 'rankings.json').write_text(json.dumps(rankings, indent=2), encoding='utf-8')
    print(f'Wrote {len(result["windows"])} windows to {args.output}')


if __name__ == '__main__':
    main()
