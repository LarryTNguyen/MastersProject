"""Windowed service ranking and arithmetic excess-time consistency checks.

This is a descriptive heuristic, not a causal diagnosis or a FIRM implementation.
Only comparable, complete canonical traces are eligible for timing analysis.
"""
from collections import Counter, defaultdict
from statistics import median

from reconstruction.trace_reconstruction.topology import group_records


def diagnose(records, *, window_seconds=300, min_samples=10, slow_ratio=1.5,
             min_excess_ms=5.0, alert_delta=0.25, assume_synchronous=False):
    if window_seconds <= 0 or min_samples < 2 or slow_ratio <= 1 or min_excess_ms < 0 or alert_delta < 0:
        raise ValueError('invalid diagnosis thresholds')
    buckets = defaultdict(list)
    excluded = Counter()
    for group in group_records(records).values():
        trace = group.reconstruct(assume_synchronous=assume_synchronous)
        if trace.critical_path is None:
            excluded[trace.quality['unavailable_reasons']['critical_path']] += 1
            continue
        nodes = {s['span_id']: s for s in trace.spans}
        root = nodes[trace.root_span_id]
        if root.get('time_domain') != 'UNIX' or any(s.get('timing_quality') != 'MEASURED' for s in trace.spans):
            excluded['requires_measured_unix_intervals'] += 1
            continue
        duration = int(root['end_time_ns']) - int(root['start_time_ns'])
        if duration <= 0:
            excluded['nonpositive_root_duration'] += 1
            continue
        service_times = defaultdict(int)
        for sid, feature in trace.timing_features.items():
            # Namespace prevents unrelated services with the same name merging.
            span = nodes[sid]
            service = '/'.join(filter(None, [span.get('service_namespace'), span['service_name']]))
            service_times[service] += int(feature['critical_path_contribution_ns'])
        width = window_seconds * 1_000_000_000
        window = int(root['start_time_ns']) // width * width
        endpoint = root['service_name'] + ' ' + root['operation_name']
        buckets[(trace.run_id, endpoint, window)].append({
            'trace_id': trace.trace_id, 'duration_ns': duration, 'services': dict(service_times),
            'root_operation': root['operation_name'],
            'service_names': {'/'.join(filter(None, [s.get('service_namespace'), s['service_name']])): s['service_name'] for s in trace.spans}})

    windows = []
    previous = {}
    for (run_id, endpoint, start), traces in sorted(buckets.items()):
        result = {'run_id': run_id, 'endpoint': endpoint, 'window_start_ns': str(start),
                  'root_operation': traces[0]['root_operation'],
                  'window_end_ns': str(start + window_seconds * 1_000_000_000),
                  'trace_count': len(traces), 'rankings': [], 'alerts': [], 'note': None}
        windows.append(result)
        if len(traces) < min_samples:
            result['note'] = 'Insufficient traces in this endpoint window.'
            continue
        baseline = median(t['duration_ns'] for t in traces)
        slow = [t for t in traces if t['duration_ns'] >= baseline * slow_ratio]
        normal = [t for t in traces if t['duration_ns'] < baseline * slow_ratio]
        result.update(slow_traces=len(slow), normal_traces=len(normal), threshold_ms=baseline * slow_ratio / 1e6)
        if not slow or not normal:
            result['note'] = 'No separable tail episode.'
            previous[(run_id, endpoint)] = (start, {})
            continue
        normal_duration = median(t['duration_ns'] for t in normal)
        slow_duration = median(t['duration_ns'] for t in slow)
        for service in sorted({s for t in traces for s in t['services']}):
            # Include zeros for absent services; avoid conditioning on presence.
            normal_time = median(t['services'].get(service, 0) for t in normal)
            slow_time = median(t['services'].get(service, 0) for t in slow)
            normal_share = median(t['services'].get(service, 0) / t['duration_ns'] for t in normal)
            slow_share = median(t['services'].get(service, 0) / t['duration_ns'] for t in slow)
            excess = max(0, slow_time - normal_time)
            score = min(1.0, excess / max(1, slow_duration - normal_duration))
            if excess < min_excess_ms * 1e6 or slow_share <= normal_share:
                continue
            adjusted = [max(0, t['duration_ns'] - max(0, t['services'].get(service, 0) - normal_time)) for t in slow]
            result['rankings'].append({
                'service': service, 'score': score,
                'service_name': next(t['service_names'][service] for t in traces if service in t['service_names']),
                'normal_critical_ms': normal_time / 1e6, 'slow_critical_ms': slow_time / 1e6,
                'normal_share': normal_share, 'slow_share': slow_share,
                'excess_ms': excess / 1e6,
                'normal_duration_ms': normal_duration / 1e6,
                'slow_duration_ms': slow_duration / 1e6,
                'adjusted_slow_duration_ms': median(adjusted) / 1e6,
                'gap_reduction_fraction': 1 - abs(median(adjusted) - normal_duration) / (slow_duration - normal_duration),
                'supporting_trace_ids': [t['trace_id'] for t in sorted(slow, key=lambda t: (-t['services'].get(service, 0), t['trace_id']))[:5]],
            })
        result['rankings'].sort(key=lambda r: (-r['score'], r['service']))
        if not result['rankings']:
            result['note'] = 'No service has sufficient excess time and an inflated critical-path share.'
        key = run_id, endpoint
        prev_start, prev_scores = previous.get(key, (None, {}))
        for rank, row in enumerate(result['rankings'], 1):
            row['rank'] = rank
            if prev_start == start - window_seconds * 1_000_000_000:
                delta = row['score'] - prev_scores.get(row['service'], 0)
                if delta >= alert_delta:
                    result['alerts'].append({'service': row['service'], 'score_delta': delta})
        previous[key] = (start, {r['service']: r['score'] for r in result['rankings']})
    return {'method': 'tail-share-excess-v1', 'is_estimate': True,
            'assumptions': ['Synchronous last-finisher critical-path estimate.',
                            'Normal traces in the same endpoint/window approximate the baseline.',
                            'Subtracting excess is an arithmetic consistency check, not an intervention.'],
            'configuration': {'window_seconds': window_seconds, 'min_samples': min_samples,
                              'slow_ratio': slow_ratio, 'min_excess_ms': min_excess_ms,
                              'alert_delta': alert_delta, 'assume_synchronous': assume_synchronous},
            'excluded_traces': dict(excluded), 'windows': windows}
