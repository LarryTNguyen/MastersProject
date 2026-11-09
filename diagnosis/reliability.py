"""Versioned, quality-gated baseline and window evidence (offline snapshots)."""
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import sqlite3
from statistics import median

from reconstruction.trace_reconstruction.topology import group_records, semantic_content
from trace_schema.validate import validate_record

ALGORITHM = 'reliability-v1'


@dataclass(frozen=True)
class Policy:
    min_samples: int = 10
    slow_ratio: float = 1.5
    error_delta: float = 0.1
    min_excess_ms: float = 5.0
    window_seconds: int = 300
    version: str = 'tail-policy-v1'

    def __post_init__(self):
        if (self.min_samples < 2 or self.window_seconds <= 0 or
                not math.isfinite(self.slow_ratio) or self.slow_ratio <= 1 or
                not 0 <= self.error_delta <= 1 or
                not math.isfinite(self.min_excess_ms) or self.min_excess_ms < 0):
            raise ValueError('invalid reliability policy')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def observations(records):
    """Validate before reconstruction; retain rejected payloads and poison their trace.

    Service latency is the longest span for that service in each trace, not the
    sum of overlapping spans and not a critical-path attribution.
    """
    valid, quarantine, poisoned = [], [], set()
    for index, record in enumerate(records):
        errors = validate_record(record)
        if errors:
            quarantine.append({'record_index': index, 'errors': errors, 'record': record})
            if (isinstance(record, dict) and isinstance(record.get('run_id'), str)
                    and isinstance(record.get('trace_id'), str)):
                poisoned.add((record.get('run_id'), record.get('trace_id')))
        else:
            valid.append(record)
    rows, excluded, conflicts = [], [], []
    groups = group_records(valid)
    for key, group in sorted(groups.items()):
        trace = group.reconstruct()
        roots = [s for s in trace.spans if s['parent_relation'] == 'ROOT']
        root = roots[0] if len(roots) == 1 else None
        reason = None
        if key in poisoned:
            reason = 'invalid_canonical_record'
        elif group.has_conflicts:
            reason = 'conflicting_records'
        elif trace.topology != 'CONNECTED':
            reason = 'topology_' + trace.topology.lower()
        elif trace.quality['inferred_parent_count']:
            reason = 'inferred_parent_relations'
        elif any(s['timing_quality'] != 'MEASURED' or s['time_domain'] != 'UNIX'
                 or s['record_kind'] != 'SPAN' for s in trace.spans):
            reason = 'requires_measured_unix_intervals'
        elif trace.quality['out_of_bounds_child_count']:
            reason = 'out_of_bounds_child'
        elif int(root['duration_ns']) <= 0:
            reason = 'nonpositive_root_duration'
        identity = {'run_id': key[0], 'trace_id': key[1],
                    'endpoint': [root['service_namespace'], root['service_name'], root['operation_name']] if root else None,
                    'start_ns': root['start_time_ns'] if root and root['time_domain'] == 'UNIX' else None}
        if reason:
            excluded.append({**identity, 'reason': reason, 'quality': trace.quality})
            continue
        services = defaultdict(list)
        # The endpoint row covers all spans for error evidence; its latency is root duration.
        services[('endpoint', root['service_namespace'], root['service_name'])] = list(trace.spans)
        for span in trace.spans:
            services[('service', span['service_namespace'], span['service_name'])].append(span)
        fingerprint = digest(sorted((semantic_content(s) for s in trace.spans), key=lambda s: s['span_id']))
        for (scope, namespace, service), spans in sorted(services.items(), key=lambda item: str(item[0])):
            errors = [s['span_id'] for s in spans if s['status']['code'] == 'ERROR']
            known = bool(errors) or all(s['status']['code'] == 'OK' for s in spans)
            rows.append({**identity, 'scope': scope, 'service_namespace': namespace, 'service_name': service,
                         'duration_ns': int(root['duration_ns']) if scope == 'endpoint' else max(int(s['duration_ns']) for s in spans),
                         'error': bool(errors) if known else None, 'error_span_ids': errors,
                         'fingerprint': fingerprint})
    # Retain both versions of conflicting payloads for later inspection.
    for run_id, trace_id in sorted(poisoned - set(groups), key=str):
        excluded.append({'run_id': run_id, 'trace_id': trace_id, 'endpoint': None,
                         'start_ns': None, 'reason': 'invalid_canonical_record', 'quality': {}})
    for record in valid:
        group = groups[(record['run_id'], record['trace_id'])]
        if semantic_content(record) != semantic_content(group.records[record['span_id']]):
            conflicts.append({'accepted': group.records[record['span_id']], 'conflicting': record})
    return rows, excluded, quarantine, conflicts


def _key(row):
    return (row['run_id'], tuple(row['endpoint']), row['scope'], row['service_namespace'], row['service_name'])


def _stats(rows):
    durations = sorted(r['duration_ns'] for r in rows)
    known = [r for r in rows if r['error'] is not None]
    errors = [r for r in known if r['error']]
    return {'sample_count': len(rows), 'median_ns': median(durations) if durations else None,
            'p95_ns': durations[math.ceil(.95 * len(rows)) - 1] if len(rows) >= 20 else None,
            'p99_ns': durations[math.ceil(.99 * len(rows)) - 1] if len(rows) >= 100 else None,
            'status_known_count': len(known), 'status_unknown_count': len(rows) - len(known),
            'error_trace_count': len(errors), 'error_rate': len(errors) / len(known) if known else None,
            'error_evidence': [{'trace_id': r['trace_id'], 'span_ids': r['error_span_ids']} for r in errors]}


def analyze(baseline_records, records, *, application, environment, policy=None, synthetic=False):
    """Compare an explicitly selected reference capture with a disjoint target capture."""
    policy = policy or Policy()
    baseline_records, records = list(baseline_records), list(records)
    def is_synthetic(record):
        if not isinstance(record, dict):
            return False
        flags = record.get('quality_flags')
        return (record.get('timing_quality') == 'SYNTHETIC' or
                isinstance(flags, list) and any('synthetic' in str(f).lower() for f in flags))

    marked_synthetic = any(is_synthetic(r) for r in baseline_records + records)
    if marked_synthetic and not synthetic:
        raise ValueError('synthetic records require the explicit synthetic label')
    base, base_excluded, base_invalid, base_conflicts = observations(baseline_records)
    target, excluded, invalid, conflicts = observations(records)
    overlap = {(r['run_id'], r['trace_id']) for r in base} & {(r['run_id'], r['trace_id']) for r in target}
    if overlap:
        raise ValueError('baseline and evaluation must contain disjoint trace IDs')
    grouped = defaultdict(list)
    for row in base:
        grouped[_key(row)].append(row)
    baselines, lookup = [], {}
    for key, rows in sorted(grouped.items(), key=lambda item: str(item[0])):
        first = rows[0]
        entry = {k: first[k] for k in ('run_id', 'endpoint', 'scope', 'service_namespace', 'service_name')}
        entry.update(_stats(rows))
        entry.update(application=application, environment=environment, algorithm_version=ALGORITHM,
                     configuration=asdict(policy), configuration_version=digest(asdict(policy)),
                     time_domain='UNIX', timing_quality='MEASURED',
                     time_start_ns=str(min(int(r['start_ns']) for r in rows)),
                     time_end_ns=str(max(int(r['start_ns']) for r in rows) + 1),
                     data_label='synthetic' if synthetic else 'user-provided; provenance requires review',
                     input_manifest=[{'trace_id': r['trace_id'], 'fingerprint': r['fingerprint']} for r in rows])
        entry['baseline_id'] = digest(entry)
        baselines.append(entry)
        lookup[key] = entry
    width = policy.window_seconds * 1_000_000_000
    windows = defaultdict(list)
    window_exclusions = defaultdict(Counter)
    for row in target:
        windows[(_key(row), int(row['start_ns']) // width * width)].append(row)
    for row in excluded:
        if row['endpoint'] is not None and row['start_ns'] is not None:
            key = (row['run_id'], tuple(row['endpoint']), 'endpoint', row['endpoint'][0], row['endpoint'][1])
            wk = key, int(row['start_ns']) // width * width
            windows[wk]  # preserve completely unavailable windows
            window_exclusions[wk][row['reason']] += 1
    output = []
    for (key, start), rows in sorted(windows.items(), key=lambda item: str(item[0])):
        baseline = lookup.get(key)
        stats = _stats(rows)
        ready = baseline is not None and baseline['sample_count'] >= policy.min_samples
        threshold = baseline['median_ns'] * policy.slow_ratio if ready else None
        slow = [r['trace_id'] for r in rows if threshold is not None and r['duration_ns'] >= threshold]
        status = ('unavailable' if not rows else 'insufficient_samples'
                  if not ready or len(rows) < policy.min_samples else 'available')
        error_ready = (baseline is not None and baseline['status_known_count'] >= policy.min_samples
                       and stats['status_known_count'] >= policy.min_samples)
        deviation = stats['error_rate'] - baseline['error_rate'] if error_ready else None
        endpoint_key = (key[0], key[1], 'endpoint', key[1][0], key[1][1])
        reasons = window_exclusions[(endpoint_key, start)]
        output.append({'run_id': key[0], 'endpoint': list(key[1]), 'scope': key[2],
                       'service_namespace': key[3], 'service_name': key[4],
                       'window_start_ns': str(start), 'window_end_ns': str(start + width),
                       'baseline_id': baseline['baseline_id'] if baseline else None,
                       **stats, 'eligible_trace_count': len(rows), 'availability': status,
                       'slow_trace_count': len(slow) if status == 'available' else None,
                       'slow_threshold_ns': threshold,
                       'slow_prevalence': len(slow) / len(rows) if status == 'available' else None,
                       'slow_trace_ids': slow, 'excluded_trace_count': sum(reasons.values()),
                       'exclusion_reasons': dict(reasons), 'baseline_error_rate': baseline['error_rate'] if baseline else None,
                       'error_rate_deviation': deviation,
                       'error_anomaly': deviation >= policy.error_delta and deviation > 0 if error_ready else None,
                       'error_availability': 'available' if error_ready else 'insufficient_known_status',
                       'input_manifest': [{'trace_id': r['trace_id'], 'fingerprint': r['fingerprint']} for r in rows]})
    return {'algorithm_version': ALGORITHM, 'application': application, 'environment': environment,
            'configuration': asdict(policy), 'data_label': 'synthetic' if synthetic else 'user-provided; provenance requires review',
            'baselines': baselines, 'windows': output, 'excluded_traces': excluded,
            'baseline_excluded_traces': base_excluded, 'invalid_records': invalid,
            'baseline_invalid_records': base_invalid, 'conflicts': conflicts, 'baseline_conflicts': base_conflicts}


def persist(path, report):
    """Content-addressed append-only SQLite equivalent of service_baselines.

    These are immutable capture snapshots, never advertised as live/current DB results.
    A changed capture or policy produces a new ID while preserving historical evidence.
    """
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('CREATE TABLE IF NOT EXISTS service_baselines (baseline_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS reliability_reports (report_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        for baseline in report['baselines']:
            conn.execute('INSERT OR IGNORE INTO service_baselines VALUES (?, ?)',
                         (baseline['baseline_id'], json.dumps(baseline, sort_keys=True)))
        report_id = digest(report)
        conn.execute('INSERT OR IGNORE INTO reliability_reports VALUES (?, ?)', (report_id, json.dumps(report, sort_keys=True)))
    return report_id
