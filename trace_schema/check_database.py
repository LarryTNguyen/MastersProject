"""Integration checks in a transaction that is rolled back.

Usage: python check_database.py --psql /path/to/psql --port 55439
Requires schema.sql already applied to an isolated empty test database.
"""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent

def literal(value):
    return "'" + value.replace("'", "''") + "'"

def payload(record):
    return literal(json.dumps(record)) + "::jsonb"

def assert_sql(expression, message):
    return f"DO $$ BEGIN IF ({expression}) IS DISTINCT FROM true THEN RAISE EXCEPTION {literal(message)}; END IF; END $$;\n"

def build_checks():
    parallel = json.loads((ROOT / 'examples/parallel-trace.json').read_text())
    alibaba = json.loads((ROOT / 'examples/alibaba-call.json').read_text())[0]
    rid, tid = parallel[0]['run_id'], parallel[0]['trace_id']
    where = f"run_id={literal(rid)} AND trace_id={literal(tid)}"
    sql = "BEGIN;\n"
    for run, identity, source in [(rid, 'OTEL', 'otlp_json'), (alibaba['run_id'], 'DATASET', 'alibaba_ms_call_graph_v2022')]:
        sql += f"INSERT INTO trace_v1.ingestion_runs (run_id,application_name,environment,source_format,identity_kind) VALUES ({literal(run)},'test-shop','test',{literal(source)},{literal(identity)});\n"
    # Child-before-parent ingestion, including an actually missing parent.
    for record in [parallel[1], parallel[2], parallel[0], alibaba]:
        sql += assert_sql(f"trace_v1.ingest_span({payload(record)}) = 'INSERTED'", 'first delivery inserts')
    sql += assert_sql(f"(SELECT revision=3 FROM trace_v1.traces WHERE {where})", 'out-of-order spans create revision 3')
    sql += assert_sql(f"(SELECT span_count=3 FROM trace_v1.trace_inventory WHERE {where})", 'trace inventory counts spans')
    duplicate = copy.deepcopy(parallel[1])
    duplicate['received_at_unix_ns'] = str(int(duplicate['received_at_unix_ns']) + 1)
    duplicate['source']['raw_reference'] = 's3://test/retry-record'
    sql += f"UPDATE trace_v1.traces SET last_changed_at=clock_timestamp()-interval '6 seconds' WHERE {where};\n"
    sql += assert_sql(f"trace_v1.ingest_span({payload(duplicate)}) = 'DUPLICATE'", 'arrival-only changes are duplicates')
    sql += assert_sql(f"(SELECT revision=3 FROM trace_v1.traces WHERE {where})", 'duplicate does not increment revision')
    sql += assert_sql(f"trace_v1.settle_trace({literal(rid)},{literal(tid)},3,'QUIET_WINDOW')", 'duplicate does not extend quiet window')
    conflict = copy.deepcopy(parallel[1])
    conflict['operation_name'] = 'conflicting-operation'
    sql += assert_sql(f"trace_v1.ingest_span({payload(conflict)}) = 'CONFLICT'", 'semantic conflict detected')
    sql += assert_sql(f"(SELECT revision=4 AND has_conflicts FROM trace_v1.traces WHERE {where})", 'conflict invalidates previous revision')
    sql += assert_sql(f"trace_v1.ingest_span({payload(conflict)}) = 'CONFLICT'", 'repeated conflict stays a conflict')
    sql += assert_sql(f"(SELECT revision=4 FROM trace_v1.traces WHERE {where})", 'repeated conflict does not churn revisions')
    sql += assert_sql(f"(SELECT operation_name='authorize' FROM trace_v1.spans WHERE {where} AND span_id={literal(parallel[1]['span_id'])})", 'conflict does not overwrite accepted data')
    sql += assert_sql(f"NOT trace_v1.settle_trace({literal(rid)},{literal(tid)},4,'QUIET_WINDOW')", 'quiet-window cannot close immediately')
    sql += f"UPDATE trace_v1.ingestion_runs SET ended_at=clock_timestamp() WHERE run_id={literal(rid)};\n"
    sql += assert_sql(f"NOT trace_v1.settle_trace({literal(rid)},{literal(tid)},3,'END_OF_INPUT')", 'stale finalizer rejected')
    sql += assert_sql(f"trace_v1.settle_trace({literal(rid)},{literal(tid)},4,'END_OF_INPUT')", 'current revision settles')
    analysis_id = '00000000-0000-4000-8000-000000000003'
    analysis_insert = f"INSERT INTO trace_v1.trace_analyses (analysis_id,run_id,trace_id,input_revision,algorithm_version,configuration,topology,root_span_id,quality,span_features) VALUES ({literal(analysis_id)},{literal(rid)},{literal(tid)},4,'test-1','{{}}','CONNECTED',{literal(parallel[0]['span_id'])},'{{}}','{{}}');\n"
    sql += analysis_insert
    sql += assert_sql("(SELECT count(*)=1 FROM trace_v1.current_trace_analyses)", 'current settled analysis is visible')
    late = copy.deepcopy(parallel[1])
    late['span_id'] = '0000000000000004'
    late['source']['record_key'] = late['span_id']
    sql += assert_sql(f"trace_v1.ingest_span({payload(late)}) = 'INSERTED'", 'late new span accepted')
    sql += assert_sql(f"(SELECT revision=5 AND state='OPEN' FROM trace_v1.traces WHERE {where})", 'late span reopens revision 5')
    sql += assert_sql(f"NOT trace_v1.settle_trace({literal(rid)},{literal(tid)},5,'QUIET_WINDOW')", 'late new span resets quiet window')
    sql += assert_sql("(SELECT count(*)=0 FROM trace_v1.current_trace_analyses)", 'stale analysis is hidden')
    stale = analysis_insert.replace(analysis_id, '00000000-0000-4000-8000-000000000004')
    sql += "DO $$ BEGIN BEGIN " + stale + " RAISE EXCEPTION 'stale analysis was accepted'; EXCEPTION WHEN raise_exception THEN IF SQLERRM <> 'Analysis requires the current settled trace revision' THEN RAISE; END IF; END; END $$;\n"
    # Constraint errors are caught in subtransactions, preserving outer rollback.
    bad = copy.deepcopy(parallel[0])
    bad['span_id'] = '0000000000000005'
    bad['duration_ns'] = '1'
    sql += f"DO $$ BEGIN BEGIN INSERT INTO trace_v1.spans(data) VALUES ({payload(bad)}); RAISE EXCEPTION 'bad duration accepted'; EXCEPTION WHEN check_violation THEN NULL; END; END $$;\n"
    # Independent missing-parent trace is retained rather than FK-rejected.
    missing = json.loads((ROOT / 'examples/missing-parent.json').read_text())[0]
    sql += assert_sql(f"trace_v1.ingest_span({payload(missing)}) = 'INSERTED'", 'missing parent is retained')
    sql += f"UPDATE trace_v1.traces SET opened_at=clock_timestamp()-interval '61 seconds' WHERE run_id={literal(rid)} AND trace_id={literal(missing['trace_id'])};\n"
    sql += assert_sql(f"trace_v1.settle_trace({literal(rid)},{literal(missing['trace_id'])},1,'HARD_TIMEOUT')", 'hard timeout works for missing-parent trace')
    sql += "ROLLBACK;\n"
    return sql

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--psql', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default='55439')
    parser.add_argument('--user', default='trace_schema_test')
    parser.add_argument('--database', default='postgres')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='trace-schema-check-') as tmp:
        path = Path(tmp) / 'check.sql'
        path.write_text(build_checks(), encoding='utf-8')
        result = subprocess.run([args.psql,'-h',args.host,'-p',args.port,'-U',args.user,'-d',args.database,
                                 '-v','ON_ERROR_STOP=1','-f',str(path)], capture_output=True, text=True)
        if result.returncode:
            print(result.stdout)
            print(result.stderr)
            raise SystemExit(result.returncode)
        print('PASS: database checks for out-of-order spans, duplicates, conflicts, settlement, late spans, stale analysis, constraints, and timeout; transaction rolled back.')
