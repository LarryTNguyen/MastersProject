# Sprint 5: reliability and evidence

Implementation status on November 3, 2026: baseline/error/prevalence analysis,
portable persistence, reliability checks, and the experiment report command are
implemented. **The non-synthetic fault experiment is not complete.** Docker's
Linux engine pipe is absent on this machine, including outside the sandbox.
No real application capture or first real diagnosis is claimed.

## Baseline and tail policy

`diagnosis.reliability` compares an explicitly selected reference capture with a
disjoint evaluation capture. Use a healthy, pre-injection capture for a controlled
experiment. Selection is the operator's responsibility; the code does not infer
health from latency or silently remove errors from the reference error rate.
Baseline and evaluation identities must belong to the same run. An unmatched
run/endpoint/service has no baseline. Application and environment are required
capture-level CLI metadata; do not combine applications/environments in one run.

Each endpoint is identified by root service namespace, service, and operation.
Service rows are partitioned by that endpoint and service namespace/name. An
endpoint sample is its root duration. A service sample is its longest observed
span in that trace. This is inclusive latency, not critical-path time, CPU time,
or a sum of overlapping spans. Absent services do not create zero samples.

Defaults (`tail-policy-v1`, configurable):

- At least 10 eligible baseline and window traces; windows are 300 seconds,
  aligned to UNIX time and assigned by root start.
- Slow means duration >= 1.5 times the baseline median. All eligible reference
  samples enter the baseline; tail samples in evaluation are retained.
- Median is available with one reference sample. Nearest-rank p95 requires 20
  samples and p99 requires 100; unavailable percentiles are null.
- An error-rate increase of at least 0.10 (10 percentage points) is an anomaly,
  provided both populations have at least the minimum known-status count.
  This is a configurable descriptive flag, not a significance test.
- The existing `python -m diagnosis` critical-path ranking keeps its configurable
  minimum 5 ms excess and requires `--assume-synchronous`. Error ranking in the
  new fault report uses error-rate deviation, independent of latency excess.
- The controlled fault report explicitly uses five samples per population, to
  match the experiment's minimum five healthy/five faulty traces. If traffic
  crosses window boundaries, collect enough samples per window; sparse windows
  remain unavailable for ranking.

Canonical validation precedes reconstruction. Invalid payloads go into the
report's quarantine with errors; any identifiable trace containing one is
excluded. Identical retries do not add samples. Conflicts retain the accepted
and conflicting payloads, preserve first-observation semantics, and exclude the
trace. Missing parents, multiple roots, cycles, unknown or inferred parent
relations, out-of-bounds children, and nonpositive root durations are excluded.
Only measured UNIX span intervals qualify. Derived, synthetic-timing,
duration-only Alibaba, and unknown-timing records remain explicit exclusions.

Known synthetic fixtures using measured-shaped intervals require `--synthetic`.
This labels all results synthetic and is only for plumbing tests. Unmarked
user input cannot be authenticated by this code; its output explicitly says
provenance requires review. The real experiment verifier rejects synthetic flags.

For errors, any explicit ERROR makes the trace/service error-positive. With no
ERROR, every relevant span must be OK to count as known success. UNSET and UNKNOWN
remain unknown. Error rate divides error-positive traces by known-status traces;
both the full eligible count and unknown count are reported. Error-positive
traces retain their error span IDs even if other spans have unknown status.

Prevalence divides slow traces by eligible traces. An available zero means no
slow traces; sparse windows have null prevalence and `insufficient_samples`;
windows with only excluded traces have `unavailable`. Exclusions with no reliable
root endpoint/UNIX start cannot be assigned to a window and stay in the top-level
exclusion list. Service-window exclusion counts are endpoint-wide quality
exclusions, since unreliable topology cannot establish service membership.

## Persistence and revisions

`baselines.sqlite` is a portable, append-only equivalent of `service_baselines`.
It stores baseline JSON and full reports under content hashes, including sample
counts, percentile values, error rates, identity, observed start-time range
(half-open), timing requirements, algorithm/configuration versions, data label,
and per-trace semantic fingerprints. Repeated writes are idempotent. Changed
semantic evidence or configuration creates a new snapshot. Historical snapshots
are retained; these offline results are never advertised as current live results.
Capture file SHA-256 values are retained by the CLI.

The existing PostgreSQL lifecycle remains responsible for live revision safety:
new late spans reopen traces and increment revision, current-analysis views hide
stale revisions, and publication requires the current settled revision. Rerun
offline analysis after receiving late spans. This sprint extends the database
integration check to assert duplicate quiet-window stability and immediate
stale-analysis invalidation. These database checks still need a live PostgreSQL
run; SQLite snapshot tests do not prove PostgreSQL or AWS behavior.

## Commands

From the repository root, with the existing virtual environment:

```powershell
.\.venv\Scripts\python.exe -m diagnosis.reliability_demo
.\.venv\Scripts\python.exe -m diagnosis.reliability_cli data/sprint5-synthetic/evaluation.jsonl --baseline data/sprint5-synthetic/baseline.jsonl --application shop --environment synthetic-test --synthetic --output data/sprint5-synthetic
```

This reproducible fixture has 20 baseline and 20 evaluation traces: baseline
endpoint median/p95 100 ms, p99 unavailable, slow threshold 150 ms, five slow
traces (25%), and five payment errors (25% versus a 0% reference error rate).
There are three baselines and three window rows: endpoint, checkout service,
and payment service. These results are **synthetic**.

For real input, replace both paths and metadata and omit `--synthetic`.
`reliability.json` contains results and excluded/quarantined evidence;
`baselines.sqlite` preserves snapshots. This command does not publish new metrics
to the dashboard; Sprint 4's existing ranking/dashboard path remains separate.

## Controlled experiment runbook

Follow [the Astronomy Shop procedure](../reconstruction/smoke/README.md) with
a pinned upstream checkout, running Docker, and canonical OTLP capture configured
under the controller's shared run ID. Preserve the runtime manifest and capture.
Generate at least five successful complete checkout traces before injection,
then at least five complete checkout traces with payment ERROR during the fault.
Clear the fault and wait for capture/settlement to finish. Injection/clearing
timestamps are flag-write times: allow flagd propagation time and inspect the
captured statuses; they are not proof of the precise service activation time.

```powershell
.\.venv\Scripts\python.exe -m diagnosis.fault_report --runtime reconstruction/.astronomy-smoke --input data/astronomy/capture.jsonl --output data/astronomy/report
```

The command first verifies canonical records, measured timing, shared run ID,
connected conflict-free topology, healthy success evidence, and faulty payment
ERROR evidence. It requires a clearing timestamp after injection. Traces crossing
a phase boundary or starting after clearing do not count toward the required
populations. Verification writes `verification.json` with supporting trace IDs.
The report writes `diagnosis.md`, `reliability.json`, and `baselines.sqlite` with
the manifest, capture hash, error span IDs, and comparison to the injected service.
Equal-scoring services remain tied; propagated errors can implicate callers as
well as payment. Missing sample/status evidence produces an explicit unavailable
diagnosis. The report does not pretend this descriptive ranking proves causality.

## Verification and remaining work

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s diagnosis/tests
.\.venv\Scripts\python.exe -m unittest discover -s reconstruction/tests -t reconstruction
.\.venv\Scripts\python.exe -m unittest discover -s ingest/tests -t ingest
.\.venv\Scripts\python.exe trace_schema/check_schema.py
.\.venv\Scripts\python.exe trace_schema/check_ingestion.py
$env:PYTHONPATH = "$PWD/dashboard"
.\.venv\Scripts\python.exe -m unittest discover -s dashboard/tests -t dashboard
```

Diagnosis tests include a real SQLite round trip, CLI round trip, sparse/zero/
unavailable windows, unknown status, semantic snapshot changes, and an integrated
duplicate/conflict/missing-parent/invalid-record fixture. Reconstruction tests
exercise individual failed-message retry and verifier rejection paths.

Still required for Sprint 5 acceptance: execute the PostgreSQL integration checks
on a disposable schema, complete the real fault run, retain its capture and
manifest, and review its generated diagnosis. See
[the evidence summary](sprint-5-evidence.md) for executed checks and limitations.

The synchronous critical path remains an estimate, same-window normal traces
in the older diagnosis remain an approximation, and arithmetic latency adjustment
is a consistency check rather than a real intervention. Local tests do not
establish AWS deployment correctness. No data is automatically deleted by the
tail policy; retention here means inclusion/exclusion from analysis with evidence
preserved, not a storage TTL.
