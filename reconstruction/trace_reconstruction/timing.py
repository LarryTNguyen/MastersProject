"""Observed timing and an explicitly assumed synchronous critical-path model."""
from __future__ import annotations


def analyze_timing(trace, *, assume_synchronous=False):
    nodes = {s['span_id']: s for s in trace.spans}
    intervals = {}
    for sid, span in nodes.items():
        if (span.get('start_time_ns') is not None and span.get('end_time_ns') is not None
                and span.get('time_domain') not in (None, 'UNKNOWN')):
            a, b = int(span['start_time_ns']), int(span['end_time_ns'])
            if b >= a:
                intervals[sid] = (a, b)
    features = {}
    for sid, span in nodes.items():
        reason = None
        if trace.topology != 'CONNECTED' or trace.quality['has_conflicts']:
            reason = 'incomplete_unknown_invalid_or_conflicting_topology'
        elif sid not in intervals:
            reason = 'no_comparable_interval'
        elif any(c not in intervals or nodes[c]['time_domain'] != span['time_domain']
                 for c in trace.children[sid]):
            reason = 'child_has_no_comparable_interval'
        covered = 0
        outside = 0
        if reason is None:
            start, end = intervals[sid]
            cursor = start
            for a, b in sorted(intervals[c] for c in trace.children[sid]):
                outside += int(a < start or b > end)
                a, b = max(start, a), min(end, b)
                if b > max(cursor, a):
                    covered += b - max(cursor, a)
                cursor = max(cursor, b)
            value = str(end - start - covered)
        else:
            value = None
        features[sid] = {
            'observed_self_time_ns': value,
            'child_interval_outside_parent_count': outside,
            'critical_path_contribution_ns': None,
            'unavailable_reasons': {'observed_self_time_ns': reason} if reason else {},
        }

    cp = None
    cp_reason = 'blocking_dependencies_not_established'
    if assume_synchronous:
        cp_reason = 'requires_connected_conflict_free_contained_intervals_in_one_time_domain'
        valid = (all(f['observed_self_time_ns'] is not None for f in features.values())
                 and not trace.quality['out_of_bounds_child_count']
                 and len({s.get('time_domain') for s in trace.spans}) == 1)
        if valid:
            # Iterative traversal avoids recursion limits on deep call trees.
            segments = []
            stack = [trace.root_span_id]
            while stack:
                sid = stack.pop()
                start, end = intervals[sid]
                cursor = end
                selected = []
                for child in sorted(trace.children[sid], key=lambda c: (-intervals[c][1], c)):
                    a, b = intervals[child]
                    if b > cursor or a == b:
                        continue
                    if cursor > b:
                        segments.append((b, cursor, sid))
                    selected.append(child)
                    cursor = a
                if cursor > start:
                    segments.append((start, cursor, sid))
                stack.extend(selected)
            contributions = {sid: 0 for sid in nodes}
            for a, b, sid in segments:
                contributions[sid] += b - a
            for sid, value in contributions.items():
                features[sid]['critical_path_contribution_ns'] = str(value)
            a, b = intervals[trace.root_span_id]
            cp = {
                'model': 'synchronous-last-finisher-v1', 'is_estimate': True,
                'assumptions': ['Parent waits for selected children.',
                                'Overlapping siblings use the last-finishing branch; parent links do not prove causality.'],
                'total_duration_ns': str(b - a),
                'segments': [{'span_id': sid, 'service_name': nodes[sid]['service_name'],
                              'start_time_ns': str(a), 'end_time_ns': str(b)}
                             for a, b, sid in sorted(segments)],
            }
    if cp is None:
        for f in features.values():
            f['unavailable_reasons']['critical_path_contribution_ns'] = cp_reason
    return features, cp, cp_reason if cp is None else None
