-- PostgreSQL 14+ baseline. Apply to a fresh database; do not rerun as a migration.
-- Execute validate.py (or an equivalent full validator) before ingest_span().
-- No extensions required. JSONB is the canonical source of span fields;
-- generated columns expose frequently queried fields without dual writes.
BEGIN;
CREATE SCHEMA trace_v1;

CREATE TABLE trace_v1.ingestion_runs (
    run_id uuid PRIMARY KEY,
    application_name text NOT NULL CHECK (application_name <> ''),
    environment text NOT NULL CHECK (environment <> ''),
    source_format text NOT NULL CHECK (source_format <> ''),
    dataset_name text,
    identity_kind text NOT NULL CHECK (identity_kind IN ('OTEL', 'DATASET')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ended_at timestamptz,
    quiet_window_ms integer NOT NULL DEFAULT 5000 CHECK (quiet_window_ms > 0),
    max_open_ms integer NOT NULL DEFAULT 60000 CHECK (max_open_ms >= quiet_window_ms),
    configuration jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(configuration) = 'object'),
    UNIQUE (run_id, identity_kind),
    CHECK (ended_at IS NULL OR ended_at >= created_at)
);

CREATE TABLE trace_v1.traces (
    run_id uuid NOT NULL REFERENCES trace_v1.ingestion_runs(run_id),
    trace_id text NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 256),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    state text NOT NULL DEFAULT 'OPEN' CHECK (state IN ('OPEN', 'SETTLED', 'TIMED_OUT')),
    first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    opened_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    settled_at timestamptz,
    settlement_reason text CHECK (settlement_reason IN ('QUIET_WINDOW', 'END_OF_INPUT', 'HARD_TIMEOUT')),
    has_conflicts boolean NOT NULL DEFAULT false,
    PRIMARY KEY (run_id, trace_id),
    CHECK ((state = 'OPEN' AND settled_at IS NULL AND settlement_reason IS NULL)
        OR (state = 'SETTLED' AND settled_at IS NOT NULL AND settlement_reason IN ('QUIET_WINDOW', 'END_OF_INPUT'))
        OR (state = 'TIMED_OUT' AND settled_at IS NOT NULL AND settlement_reason = 'HARD_TIMEOUT'))
);

-- Only arrival metadata is excluded from equality. All semantic/provenance
-- fields, including adapter_version, participate in conflict detection.
CREATE FUNCTION trace_v1.canonical_content(p jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT (p - 'received_at_unix_ns') #- '{source,raw_reference}'
$$;

CREATE TABLE trace_v1.spans (
    data jsonb NOT NULL CHECK (jsonb_typeof(data) = 'object'),
    run_id uuid GENERATED ALWAYS AS ((data->>'run_id')::uuid) STORED NOT NULL,
    identity_kind text GENERATED ALWAYS AS (data->>'identity_kind') STORED NOT NULL,
    trace_id text GENERATED ALWAYS AS (data->>'trace_id') STORED NOT NULL,
    span_id text GENERATED ALWAYS AS (data->>'span_id') STORED NOT NULL,
    parent_span_id text GENERATED ALWAYS AS (data->>'parent_span_id') STORED,
    parent_relation text GENERATED ALWAYS AS (data->>'parent_relation') STORED NOT NULL,
    service_namespace text GENERATED ALWAYS AS (data->>'service_namespace') STORED NOT NULL,
    service_name text GENERATED ALWAYS AS (data->>'service_name') STORED NOT NULL,
    service_instance_id text GENERATED ALWAYS AS (data->>'service_instance_id') STORED,
    operation_name text GENERATED ALWAYS AS (data->>'operation_name') STORED,
    record_kind text GENERATED ALWAYS AS (data->>'record_kind') STORED NOT NULL,
    span_kind text GENERATED ALWAYS AS (data->>'span_kind') STORED NOT NULL,
    start_time_ns bigint GENERATED ALWAYS AS ((data->>'start_time_ns')::bigint) STORED,
    end_time_ns bigint GENERATED ALWAYS AS ((data->>'end_time_ns')::bigint) STORED,
    duration_ns bigint GENERATED ALWAYS AS ((data->>'duration_ns')::bigint) STORED,
    time_domain text GENERATED ALWAYS AS (data->>'time_domain') STORED NOT NULL,
    timing_quality text GENERATED ALWAYS AS (data->>'timing_quality') STORED NOT NULL,
    status_code text GENERATED ALWAYS AS (data#>>'{status,code}') STORED NOT NULL,
    received_at_unix_ns bigint GENERATED ALWAYS AS ((data->>'received_at_unix_ns')::bigint) STORED NOT NULL,
    PRIMARY KEY (run_id, trace_id, span_id),
    FOREIGN KEY (run_id, trace_id) REFERENCES trace_v1.traces(run_id, trace_id),
    FOREIGN KEY (run_id, identity_kind) REFERENCES trace_v1.ingestion_runs(run_id, identity_kind),
    CHECK (data->>'schema_version' = '1.0.0'),
    CHECK (length(trace_id) BETWEEN 1 AND 256 AND length(span_id) BETWEEN 1 AND 256),
    CHECK (parent_span_id IS NULL OR length(parent_span_id) BETWEEN 1 AND 256),
    CHECK (service_name <> ''),
    CHECK (record_kind IN ('SPAN', 'CALL_OBSERVATION')),
    CHECK (span_kind IN ('INTERNAL', 'SERVER', 'CLIENT', 'PRODUCER', 'CONSUMER', 'UNKNOWN')),
    CHECK (status_code IN ('UNSET', 'OK', 'ERROR', 'UNKNOWN')),
    CHECK (parent_relation IN ('ROOT', 'EXPLICIT', 'INFERRED', 'UNKNOWN')),
    CHECK ((parent_relation IN ('ROOT', 'UNKNOWN')) = (parent_span_id IS NULL)),
    CHECK (parent_span_id IS NULL OR parent_span_id <> span_id),
    CHECK (identity_kind <> 'OTEL' OR (record_kind = 'SPAN'
        AND trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)
        AND span_id ~ '^[0-9a-f]{16}$' AND span_id <> repeat('0', 16)
        AND (parent_span_id IS NULL OR (parent_span_id ~ '^[0-9a-f]{16}$' AND parent_span_id <> repeat('0', 16))))),
    CHECK (start_time_ns IS NULL OR start_time_ns >= 0),
    CHECK (end_time_ns IS NULL OR end_time_ns >= 0),
    CHECK (duration_ns IS NULL OR duration_ns >= 0),
    CHECK (received_at_unix_ns >= 0),
    CHECK ((start_time_ns IS NULL) = (end_time_ns IS NULL)),
    CHECK (start_time_ns IS NULL OR (end_time_ns >= start_time_ns AND duration_ns IS NOT NULL AND duration_ns = end_time_ns - start_time_ns)),
    CHECK (time_domain IN ('UNIX', 'RELATIVE', 'UNKNOWN')),
    CHECK ((time_domain = 'UNKNOWN') = (start_time_ns IS NULL)),
    CHECK (timing_quality IN ('MEASURED', 'DERIVED', 'DURATION_ONLY', 'SYNTHETIC', 'UNKNOWN')),
    CHECK ((timing_quality IN ('MEASURED', 'DERIVED', 'SYNTHETIC')) = (start_time_ns IS NOT NULL)),
    CHECK (timing_quality <> 'DURATION_ONLY' OR duration_ns IS NOT NULL),
    CHECK (timing_quality <> 'UNKNOWN' OR duration_ns IS NULL)
    -- No parent FK: parents may arrive later, be sampled out, or never arrive.
);
CREATE INDEX spans_parent ON trace_v1.spans (run_id, trace_id, parent_span_id);
CREATE INDEX spans_service_time ON trace_v1.spans (run_id, service_namespace, service_name, start_time_ns);
CREATE INDEX traces_open_quiet ON trace_v1.traces (last_changed_at) WHERE state = 'OPEN';
CREATE INDEX traces_open_deadline ON trace_v1.traces (opened_at) WHERE state = 'OPEN';

CREATE TABLE trace_v1.trace_analyses (
    analysis_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    trace_id text NOT NULL,
    input_revision bigint NOT NULL CHECK (input_revision > 0),
    algorithm_version text NOT NULL CHECK (algorithm_version <> ''),
    configuration jsonb NOT NULL CHECK (jsonb_typeof(configuration) = 'object'),
    computed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    topology text NOT NULL CHECK (topology IN ('CONNECTED', 'INCOMPLETE', 'INVALID', 'UNKNOWN')),
    root_span_id text,
    observed_start_ns bigint CHECK (observed_start_ns >= 0),
    observed_end_ns bigint CHECK (observed_end_ns >= 0),
    root_duration_ns bigint CHECK (root_duration_ns >= 0),
    quality jsonb NOT NULL CHECK (jsonb_typeof(quality) = 'object'),
    span_features jsonb NOT NULL CHECK (jsonb_typeof(span_features) = 'object'),
    critical_path jsonb CHECK (jsonb_typeof(critical_path) = 'object'),
    FOREIGN KEY (run_id, trace_id) REFERENCES trace_v1.traces(run_id, trace_id),
    CHECK ((observed_start_ns IS NULL) = (observed_end_ns IS NULL)),
    CHECK (observed_start_ns IS NULL OR observed_end_ns >= observed_start_ns)
);

CREATE TABLE trace_v1.service_baselines (
    baseline_id uuid PRIMARY KEY,
    training_run_id uuid NOT NULL REFERENCES trace_v1.ingestion_runs(run_id),
    application_name text NOT NULL,
    environment text NOT NULL,
    endpoint_key text NOT NULL,
    service_namespace text NOT NULL,
    service_name text NOT NULL,
    operation_key text NOT NULL,
    metric_kind text NOT NULL CHECK (metric_kind IN ('OBSERVED_SELF_TIME', 'SPAN_DURATION', 'CALL_DURATION')),
    training_start_ns bigint NOT NULL CHECK (training_start_ns >= 0),
    training_end_ns bigint NOT NULL CHECK (training_end_ns > training_start_ns),
    training_time_domain text NOT NULL CHECK (training_time_domain IN ('UNIX', 'RELATIVE')),
    sample_count bigint NOT NULL CHECK (sample_count > 0),
    median_ns numeric(30,6) NOT NULL CHECK (median_ns >= 0),
    mad_ns numeric(30,6) NOT NULL CHECK (mad_ns >= 0),
    p95_ns numeric(30,6) NOT NULL CHECK (p95_ns >= 0),
    error_observed_count bigint NOT NULL CHECK (error_observed_count >= 0),
    error_count bigint NOT NULL CHECK (error_count >= 0 AND error_count <= error_observed_count),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    method_version text NOT NULL,
    configuration jsonb NOT NULL CHECK (jsonb_typeof(configuration) = 'object'),
    CHECK (error_observed_count <= sample_count)
);

CREATE TABLE trace_v1.service_window_results (
    result_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES trace_v1.ingestion_runs(run_id),
    window_start_ns bigint NOT NULL CHECK (window_start_ns >= 0),
    window_end_ns bigint NOT NULL CHECK (window_end_ns > window_start_ns),
    time_domain text NOT NULL CHECK (time_domain IN ('UNIX', 'RELATIVE')),
    endpoint_key text NOT NULL,
    service_namespace text NOT NULL,
    service_name text NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    algorithm_version text NOT NULL,
    configuration jsonb NOT NULL CHECK (jsonb_typeof(configuration) = 'object'),
    input_manifest jsonb NOT NULL CHECK (jsonb_typeof(input_manifest) = 'array'),
    signals jsonb NOT NULL CHECK (jsonb_typeof(signals) = 'object'),
    score double precision CHECK (score >= 0 AND score <= 1),
    confidence text NOT NULL CHECK (confidence IN ('LOW', 'MEDIUM', 'HIGH', 'UNAVAILABLE')),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'array'),
    baseline_ids uuid[] NOT NULL DEFAULT '{}'
    -- Application validates every referenced baseline and input revision.
    -- A manifest item is {trace_id, revision, analysis_id}; namespace is run_id.
);
CREATE INDEX analyses_trace_revision ON trace_v1.trace_analyses (run_id, trace_id, input_revision, computed_at DESC);
CREATE INDEX window_results_search ON trace_v1.service_window_results (run_id, endpoint_key, window_start_ns);

CREATE FUNCTION trace_v1.guard_trace_analysis() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_revision bigint;
    v_state text;
BEGIN
    SELECT revision, state INTO v_revision, v_state FROM trace_v1.traces
    WHERE run_id = NEW.run_id AND trace_id = NEW.trace_id FOR UPDATE;
    IF NOT FOUND OR NEW.input_revision <> v_revision OR v_state = 'OPEN' THEN
        RAISE EXCEPTION 'Analysis requires the current settled trace revision';
    END IF;
    IF NEW.root_span_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM trace_v1.spans WHERE run_id = NEW.run_id AND trace_id = NEW.trace_id
          AND span_id = NEW.root_span_id AND parent_relation = 'ROOT'
    ) THEN
        RAISE EXCEPTION 'Analysis root must be an observed root in this trace';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_trace_analysis BEFORE INSERT ON trace_v1.trace_analyses
FOR EACH ROW EXECUTE FUNCTION trace_v1.guard_trace_analysis();

CREATE FUNCTION trace_v1.reject_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Published records are immutable; insert a new version';
END $$;
CREATE TRIGGER immutable_trace_analysis BEFORE UPDATE ON trace_v1.trace_analyses
FOR EACH ROW EXECUTE FUNCTION trace_v1.reject_update();
CREATE TRIGGER immutable_baseline BEFORE UPDATE ON trace_v1.service_baselines
FOR EACH ROW EXECUTE FUNCTION trace_v1.reject_update();
CREATE TRIGGER immutable_window_result BEFORE UPDATE ON trace_v1.service_window_results
FOR EACH ROW EXECUTE FUNCTION trace_v1.reject_update();

CREATE VIEW trace_v1.current_trace_analyses AS
SELECT a.* FROM trace_v1.trace_analyses a
JOIN trace_v1.traces t USING (run_id, trace_id)
WHERE a.input_revision = t.revision AND t.state <> 'OPEN';

CREATE VIEW trace_v1.trace_inventory AS
SELECT t.*, x.span_count, x.error_span_count, x.observed_status_count,
       x.observed_start_ns, x.observed_end_ns
FROM trace_v1.traces t
LEFT JOIN LATERAL (
    SELECT count(*) AS span_count,
           count(*) FILTER (WHERE status_code = 'ERROR') AS error_span_count,
           count(*) FILTER (WHERE status_code IN ('OK', 'ERROR')) AS observed_status_count,
           min(start_time_ns) AS observed_start_ns, max(end_time_ns) AS observed_end_ns
    FROM trace_v1.spans s WHERE s.run_id = t.run_id AND s.trace_id = t.trace_id
) x ON true;

CREATE FUNCTION trace_v1.ingest_span(p jsonb) RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    v_run uuid := (p->>'run_id')::uuid;
    v_trace text := p->>'trace_id';
    v_span text := p->>'span_id';
    existing jsonb;
    v_now timestamptz;
BEGIN
    -- Trusted application must fully validate p first. Parent/cycle validation
    -- belongs to reconstruction, not ingestion of an individual span.
    INSERT INTO trace_v1.traces (run_id, trace_id) VALUES (v_run, v_trace)
    ON CONFLICT (run_id, trace_id) DO NOTHING;
    PERFORM 1 FROM trace_v1.traces WHERE run_id = v_run AND trace_id = v_trace FOR UPDATE;
    v_now := clock_timestamp();
    SELECT data INTO existing FROM trace_v1.spans
    WHERE run_id = v_run AND trace_id = v_trace AND span_id = v_span;
    IF FOUND THEN
        IF trace_v1.canonical_content(existing) = trace_v1.canonical_content(p) THEN
            UPDATE trace_v1.traces SET last_received_at = v_now WHERE run_id = v_run AND trace_id = v_trace;
            RETURN 'DUPLICATE';
        END IF;
        -- First accepted record stays unchanged. Raw archive/quarantine retains
        -- the conflicting observation; caller must route CONFLICT there.
        UPDATE trace_v1.traces SET
            revision = revision + CASE WHEN has_conflicts THEN 0 ELSE 1 END,
            opened_at = CASE WHEN NOT has_conflicts AND state <> 'OPEN' THEN v_now ELSE opened_at END,
            state = CASE WHEN NOT has_conflicts THEN 'OPEN' ELSE state END,
            settled_at = CASE WHEN NOT has_conflicts THEN NULL ELSE settled_at END,
            settlement_reason = CASE WHEN NOT has_conflicts THEN NULL ELSE settlement_reason END,
            last_changed_at = CASE WHEN NOT has_conflicts THEN v_now ELSE last_changed_at END,
            last_received_at = v_now, has_conflicts = true
        WHERE run_id = v_run AND trace_id = v_trace;
        RETURN 'CONFLICT';
    END IF;
    INSERT INTO trace_v1.spans (data) VALUES (p);
    UPDATE trace_v1.traces SET revision = revision + 1,
        opened_at = CASE WHEN state <> 'OPEN' THEN v_now ELSE opened_at END,
        state = 'OPEN', settled_at = NULL, settlement_reason = NULL,
        last_changed_at = v_now, last_received_at = v_now
    WHERE run_id = v_run AND trace_id = v_trace;
    RETURN 'INSERTED';
END $$;

CREATE FUNCTION trace_v1.settle_trace(p_run uuid, p_trace text, p_revision bigint, p_reason text)
RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE
    v_count integer;
BEGIN
    IF p_reason NOT IN ('QUIET_WINDOW', 'END_OF_INPUT', 'HARD_TIMEOUT') THEN
        RAISE EXCEPTION 'Unsupported settlement reason: %', p_reason;
    END IF;
    UPDATE trace_v1.traces t SET
        state = CASE WHEN p_reason = 'HARD_TIMEOUT' THEN 'TIMED_OUT' ELSE 'SETTLED' END,
        settled_at = clock_timestamp(), settlement_reason = p_reason
    FROM trace_v1.ingestion_runs r
    WHERE t.run_id = p_run AND t.trace_id = p_trace AND r.run_id = t.run_id
      AND t.revision = p_revision AND t.state = 'OPEN'
      AND ((p_reason = 'QUIET_WINDOW' AND t.last_changed_at + r.quiet_window_ms * interval '1 millisecond' <= clock_timestamp())
        OR (p_reason = 'HARD_TIMEOUT' AND t.opened_at + r.max_open_ms * interval '1 millisecond' <= clock_timestamp())
        OR (p_reason = 'END_OF_INPUT' AND r.ended_at IS NOT NULL));
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count = 1;
END $$;
COMMIT;
