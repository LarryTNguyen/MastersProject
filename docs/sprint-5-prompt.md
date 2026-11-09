# Sprint 5 Prompt: Reliability and Evidence

## Project

Serverless Trace Analyzer — Master's Project

## Sprint dates

November 2–13, 2026

## Sprint theme

Reliability and evidence

## Sprint objective

Strengthen the trace-analysis pipeline so that it can recognize unreliable or incomplete input, preserve the evidence needed for later diagnosis, and produce a defensible first diagnosis from latency and error data.

The Sprint 4 implementation provides the main handoff: canonical trace normalization, trace reconstruction, revisioned analysis, quiet-window settlement, critical-path estimation, bottleneck ranking, dashboard integration, and a synthetic diagnosis demo. Sprint 5 should build reliability and evidence features on top of that foundation.

## Assigned work

The primary Sprint 5 work is to:

1. Define latency and error baselines.
2. Establish a policy for retaining or excluding tail and unreliable traces.
3. Handle duplicate, late, incomplete, and failed records consistently.
4. Support late-span reprocessing and stale-analysis invalidation.
5. Calculate slow-trace prevalence and anomaly indicators.
6. Run a fault-injection experiment and produce the first evidence-backed diagnosis.

## Current repository starting point

The repository already contains:

- Canonical span normalization for Alibaba, RCAEval, and OTLP inputs.
- Duplicate and conflict handling in the PostgreSQL ingestion function.
- Revision increments when new semantic spans arrive.
- Late-span reopening of settled traces.
- Quiet-window, end-of-input, and hard-timeout settlement paths.
- Missing-parent and incomplete-topology detection.
- Per-message retry reporting for failed ingestion messages.
- Synthetic tail-episode diagnosis using median latency and critical-path excess.
- An Astronomy Shop payment-failure smoke-test controller.

These features make the project ready to begin Sprint 5, but they do not yet satisfy every Sprint 5 deliverable.

## Requirements

### 1. Reliability handling

Verify that the system handles each condition below without silently turning missing or invalid evidence into valid-looking data.

#### Duplicate records

- Identical records with different arrival metadata must be classified as duplicates.
- Duplicates must not create a new span or change the trace revision.
- Duplicate delivery must not extend the quiet window unnecessarily.

#### Conflicting records

- Records with the same identity but different semantic content must be classified as conflicts.
- The first accepted record must remain unchanged.
- The conflict must be retained or routed to quarantine for later inspection.
- The trace must be marked as having conflicts.

#### Late spans

- A new span received after settlement must reopen the trace.
- The trace revision must increase.
- Any previous analysis for the older revision must become stale.
- The trace must be re-settled and re-analyzed before publication.

#### Incomplete traces

- Missing parents, multiple roots, unknown parent relations, and invalid topology must remain explicit.
- Incomplete traces must not be used for analyses that require connected timing data.
- The reason for excluding an incomplete trace must be visible in the quality output.

#### Failed records or messages

- Invalid messages must be reported individually so that valid messages in the same batch can succeed.
- Failed messages must be returned for retry or dead-letter processing.
- Invalid canonical records must not reach reconstruction or diagnosis.

### 2. Latency baseline

Implement a documented baseline policy for each endpoint and service.

At minimum, record:

- sample count;
- median latency;
- p95 and p99 latency where enough samples exist;
- baseline time range;
- endpoint and service identity;
- run, application, and environment identifiers;
- timing domain and timing-quality requirements;
- the algorithm and configuration version used to produce the baseline.

The baseline must exclude traces that are incomplete, invalid, duration-only when measured intervals are required, or otherwise unavailable for the selected analysis.

The baseline should be persisted using the existing `service_baselines` table or an equivalent versioned representation.

### 3. Error baseline and anomaly detection

Add explicit error analysis instead of relying only on latency ranking.

For each endpoint and service, calculate:

- total eligible traces;
- traces containing an error;
- error rate;
- baseline error rate;
- error-rate deviation or anomaly flag;
- supporting trace IDs and relevant error spans.

An error anomaly must identify the evidence used to make the determination. Missing status information must remain distinct from a successful status.

### 4. Slow-trace prevalence

Calculate the prevalence of slow traces relative to the selected latency baseline.

At minimum, report:

- eligible trace count;
- slow trace count;
- slow-trace threshold;
- slow-trace prevalence;
- endpoint and time window;
- excluded trace count and reasons.

The implementation must clearly distinguish between:

- a window with no slow traces;
- a window with insufficient samples;
- a window where diagnosis is unavailable because timing evidence is incomplete.

### 5. Tail-trace policy

Document and implement the policy for keeping or excluding traces from diagnosis.

The policy should state:

- how the normal baseline is selected;
- how a trace becomes classified as slow;
- the minimum sample count;
- the minimum service excess required for ranking;
- how incomplete or conflicting traces are handled;
- whether synthetic traces are allowed in evaluation results;
- how duration-only Alibaba observations are treated;
- how the policy changes when data is sparse.

The current Sprint 4 diagnosis defaults are a starting point: a 1.5× median slow-trace threshold, at least 10 traces per window, and at least 5 ms of service excess. These values must be treated as configuration, not universal truths.

### 6. First evidence-backed diagnosis

Run at least one controlled fault-injection experiment using the Astronomy Shop payment-failure path or another approved microservice application.

The experiment should include:

- at least five healthy checkout traces;
- at least five traces captured during the fault window;
- one shared run ID;
- a recorded injection timestamp;
- a recorded fault-clearing timestamp;
- canonical JSONL output;
- verification that the faulty traces contain payment-service error evidence;
- topology validation;
- a diagnosis report containing supporting trace IDs;
- a comparison between the injected root cause and the top-ranked service.

Synthetic diagnosis output may be used for plumbing tests, but it must be labeled synthetic and must not be presented as evidence from a real application run.

## Suggested implementation order

1. Run the current diagnosis, reconstruction, and ingestion test suites.
2. Install missing development dependencies and run schema and dashboard tests.
3. Add an integrated reliability fixture containing duplicates, conflicts, late spans, missing parents, and failed messages.
4. Implement baseline data structures and persistence.
5. Implement error-rate and slow-trace-prevalence calculations.
6. Add unit tests for sparse windows, unavailable status, incomplete topology, and stale revisions.
7. Run the Astronomy Shop payment-failure experiment.
8. Generate the first diagnosis report and compare it with the known injected fault.
9. Update the README or create `docs/sprint-5.md` with commands, assumptions, limitations, and evidence.

## Acceptance criteria

Sprint 5 is complete when:

- [ ] Duplicate records are idempotent and tested.
- [ ] Conflicts are preserved or quarantined and tested.
- [ ] Late spans reopen traces and invalidate stale analyses.
- [ ] Incomplete traces are classified and excluded from unsupported analyses.
- [ ] Failed messages are retried individually without losing valid messages.
- [ ] Latency baselines are calculated, versioned, and persisted.
- [ ] Error baselines and error anomalies are calculated.
- [ ] Slow-trace prevalence is reported with denominators and exclusions.
- [ ] Tail-retention and exclusion rules are documented.
- [ ] At least one non-synthetic fault experiment is completed.
- [ ] The experiment has at least five healthy and five faulty checkout traces.
- [ ] Faulty traces contain the expected payment-service error evidence.
- [ ] A diagnosis report identifies the injected service or clearly explains why diagnosis was unavailable.
- [ ] All relevant unit and integration tests pass.
- [ ] The final report distinguishes measured, derived, synthetic, incomplete, and unavailable evidence.

## Important limitations to document

- The current critical-path diagnosis is a synchronous estimate, not observed causality.
- Same-window normal traces are only an approximation of a production baseline.
- Arithmetic adjustment of slow latency is a consistency check, not a real intervention.
- Alibaba duration-only observations cannot support measured interval analysis without additional timing information.
- Local repository tests do not prove AWS deployment correctness.
- The Astronomy Shop controller prepares and verifies an experiment, but the experiment itself must still be executed.

## Expected Sprint 5 deliverables

1. Baseline calculation and persistence implementation.
2. Error-anomaly and slow-trace-prevalence implementation.
3. Integrated reliability tests.
4. Fault-injection experiment data and manifest.
5. First non-synthetic diagnosis report.
6. Updated documentation and runbook.
7. Test results and a short evidence summary for the project workbook.

