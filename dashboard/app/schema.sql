-- Proposed tables the dashboard reads. SQLite for local development; the same
-- statements run on Aurora PostgreSQL (change TEXT attributes/evidence to JSONB there).
--
-- spans    : written by the reconstruction step (one row per span)
-- traces   : one row per reconstructed trace
-- ranking_windows : written by the diagnosis step, one row per analysis window
--                   (a window with no ranked services and a note is the "no clear bottleneck" result)
-- rankings        : one row per ranked service inside a window

CREATE TABLE IF NOT EXISTS spans (
    trace_id        TEXT NOT NULL,
    span_id         TEXT NOT NULL,
    parent_span_id  TEXT,
    service         TEXT NOT NULL,
    operation       TEXT NOT NULL,
    start_us        BIGINT NOT NULL,
    duration_us     BIGINT,
    status          TEXT NOT NULL DEFAULT 'unset',
    kind            TEXT,
    peer_service    TEXT,
    source          TEXT NOT NULL DEFAULT 'unknown',
    attributes      TEXT,
    received_us     BIGINT,                   -- when the raw file landed in S3 (sta.received_us)
    normalized_us   BIGINT,                   -- when the adapter wrote the span (sta.normalized_us)
    PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS spans_service_idx ON spans (service, start_us);

CREATE TABLE IF NOT EXISTS traces (
    trace_id        TEXT PRIMARY KEY,
    root_service    TEXT NOT NULL,
    root_operation  TEXT NOT NULL,
    start_us        BIGINT NOT NULL,
    duration_us     BIGINT NOT NULL,
    span_count      INTEGER NOT NULL,
    service_count   INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'unset',
    complete        INTEGER NOT NULL DEFAULT 1,
    source          TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS traces_start_idx ON traces (start_us);
CREATE INDEX IF NOT EXISTS traces_duration_idx ON traces (duration_us);

CREATE TABLE IF NOT EXISTS trace_services (
    trace_id        TEXT NOT NULL,
    service         TEXT NOT NULL,
    PRIMARY KEY (trace_id, service)
);

CREATE TABLE IF NOT EXISTS ranking_windows (
    window_id       TEXT PRIMARY KEY,         -- "<start_us>:<end_us>:<endpoint>:<method>"
    window_start_us BIGINT NOT NULL,
    window_end_us   BIGINT NOT NULL,
    endpoint        TEXT,
    method          TEXT NOT NULL DEFAULT 'bottleneck_score',
    confidence      REAL,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS ranking_windows_end_idx ON ranking_windows (window_end_us);

CREATE TABLE IF NOT EXISTS rankings (
    window_id       TEXT NOT NULL,
    rank            INTEGER NOT NULL,
    service         TEXT NOT NULL,
    score           REAL NOT NULL,
    evidence        TEXT,
    PRIMARY KEY (window_id, rank)
);
