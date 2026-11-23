# Bottleneck score v2: implementation and evidence

The November 19, 2026 checkout was clean before this change. Inspection confirmed
that v1 computes median excess critical-path contribution in slow versus normal
traces inside each endpoint/window, divides by endpoint excess (capped at one),
and drops a service unless excess is at least 5 ms and its path share increases.
It needs ten traces by default, a separable 1.5x-median tail, contained intervals,
and the explicit synchronous assumption. Reliability already provides disjoint
reference/evaluation comparisons, known-status denominators, and portable snapshots.
The fault verifier requires measured, complete, non-synthetic checkout traces.
The runner already supports external ranking callables, but had no MicroRCA adapter.

The saved 240-case v1 report has acc@1 .15, acc@3 .31, MRR .23, and the true
service absent in 152 cases. This is historical evidence, not a rerun. Inspection
also confirmed the old RCAEval adapter overwrote DERIVED with MEASURED. That has
been removed. Production reliability defaults remain measured and contained.

## Formula and evidence

`diagnosis.score_v2` is separately versioned as `bottleneck-score-v2`; v1 remains
available. V2 requires an operator-selected reference capture in the same run and
endpoint, with disjoint trace IDs. Pre-injection RCAEval traces supply the reference;
this assumes health rather than inferring it from the ground-truth service label.
Service samples are the longest inclusive span per trace, without zero samples for
absent services. Every observed eligible service remains a candidate, including
zero-score services. A missing reference gives null scores and explicit reasons;
the benchmark adapter does not turn these into a fabricated ordering.

Initial weights are critical path .30, inclusive latency .30, slow prevalence .15,
and errors .25. They are configurable and were not fitted to these fixtures.
For latency and critical path, let b be the reference median, t the window median,
and d=max(0,t-b). The normalized signal is d/(max(b,1 ms)+d). The 1 ms floor is
configurable. Prevalence is the positive change in the fraction of service traces
at least 1.5 times the reference median. Errors use the positive change in error
rate, dividing only by traces with known status. Any ERROR is positive; success
requires every service span to be OK. UNSET/UNKNOWN remain unknown. All signals
are in [0,1]; missing signals have null values, and available weights are divided
by their sum. No resource signal is present.

Each signal records its raw change, normalized value, reference value, counts,
availability, reason, and reference/window trace and span IDs, including error span
IDs and timing-quality labels. Reports record algorithm/configuration hashes,
input and reference hashes, timing policy, exclusions, quarantine and conflicts.

Confidence is separate: weighted sample support (min(reference count, window count,
minimum samples)/minimum samples, capped at one) times trace quality times available
weight coverage. Trace quality applies .75 for derived and .5 for uncontained
observations and the minimum baseline/window eligible-trace retention. Below .8 is
labelled low. These are initial descriptive settings, not calibrated probabilities.
Exclusions without an identifiable endpoint/start remain in the global exclusion
list and cannot be assigned to a window's retention denominator.

Uncontained but connected, conflict-free spans can supply inclusive duration and
status evidence in v2. They cannot supply critical-path evidence. Disconnected,
conflicting, invalid, inferred-parent, nonpositive-root, and unsupported-timing
traces remain excluded. This retains more usable evidence without repairing clocks
or fabricating causal paths. Critical path remains a synchronous estimate. Caller
waiting gaps stay with the caller; a regression test ensures they are not silently
assigned to a callee. Errors may propagate, and a high score does not prove cause.
Services with no spans cannot be recovered; evaluation evidence explicitly records
whether the labelled service is present.

## Commands

From the repo root, using PowerShell:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m diagnosis.score_v2 data/sprint5-synthetic/evaluation.jsonl --baseline data/sprint5-synthetic/baseline.jsonl --output data/score-v2-demo --assume-synchronous
```

This is synthetic plumbing evidence. `score-v2.json` contains all candidates and
signals. Copy the generated `rankings.json` beside the corresponding evaluation
spans to view scored candidates with evidence in the existing dashboard. Null-score
candidates stay in the detailed report. Baseline and evaluation must share run and
endpoint identity. `--weights` accepts a JSON object with all four weight names.

Canonical CLI defaults accept measured UNIX intervals only. Explicitly opt in with
`--timing-policy measured-or-derived` for a labelled derived-interval investigation.
V1's CLI now exposes the same option. Neither version changes source timing labels.
The benchmark `tail_share_excess` and `score_v2` methods accept derived intervals;
`v1_measured` and `v2_measured` are separate measured-only comparisons.

```powershell
.\.venv\Scripts\python.exe -X utf8 -m experiments.run experiments/score_comparison.json
```

The full 240 cases are absent locally. Downloading them requires the dataset tools
and roughly 2.6 GB of traces; see `experiments/get_rcaeval.py`. This command should
only be reported as a full comparison after those cases are available. For tuning,
set `rcaeval.split` to `validation`, then freeze weights/configuration and run
`held-out`. SHA-256 of the case name without its repetition number chooses 20%
validation groups; benchmark/service/fault repetitions stay together. This is a
reproducible grouped split, not a claim of generalization to unseen service types.
No tuning was performed here. The direct evaluator also accepts `--split`.

The evaluator saves complete rankings/evidence and input file hashes in
`rcaeval_evidence.json`, CSV results, acc@1/3, MRR, true-service-not-found counts,
no-ranking counts, and per-benchmark/per-fault breakdowns. Ties are deterministically
ordered by service name for benchmark positions; this ordering is not causal evidence.
V1 retains its within-window reference; v2 uses the supplied pre-injection reference.

## MicroRCA status

Reference inspected: [elastisys/MicroRCA](https://github.com/elastisys/MicroRCA/blob/de334b2815742a248225cf6e242840a9d5fedc9a/MicroRCA.py),
revision `de334b2815742a248225cf6e242840a9d5fedc9a`. It reads source/destination
latency CSVs, a service/host graph, and per-service container and node CPU, memory
and network series. It uses pandas, NumPy, NetworkX and scikit-learn. No compatible
resource series or host mapping is present in this checkout's trace fixtures or
captures. The generic hook alone cannot execute a valid comparison. No MicroRCA
adapter, fabricated metrics, or MicroRCA results are included.

To collect the missing inputs, pin a demo checkout and record its commit and Compose
configuration; enable Prometheus collection from container and host exporters
(cAdvisor and node-exporter, or equivalent OTel receivers). Keep timestamped
CPU utilization, memory utilization and network rates for every service container
and its host, with container/service/host mappings. Collect inter-service round-trip
latency (service-mesh telemetry or span metrics with explicit client/server roles).
Capture healthy, injected and recovery intervals on the same clock. Export all
series on a common fixed sampling grid; do not fill missing series with fabricated
zeros. Preserve raw queries, units, timestamps and mappings. Audit coverage and
reference input schemas before implementing conversion. Freeze dependencies in an
isolated environment compatible with this older pandas API, and tune MicroRCA's
thresholds only on the validation groups. Merely downloading RCAEval metrics via
`get_rcaeval --with-metrics` does not establish the required schema or host coverage.

## Astronomy Shop status

Docker's Linux engine pipe is missing on this machine. No real fault run completed.
The existing private-flag/Compose workflow remains in use. Follow
`reconstruction/smoke/README.md` with an upstream checkout pinned to an exact commit;
record `git rev-parse HEAD` and verify it matches the version passed to `prepare`.
The controller records the supplied version; it does not attest that checkout.
Use one run ID, five complete healthy checkouts, then five complete faulty-window
checkouts with paymentFailure at 100%, clear, settle and export canonical JSONL.

`diagnosis.fault_report` still verifies first and produces its error-deviation
comparison. It now also runs v1 on the faulty capture and v2 using the verified
healthy reference (minimum samples five), writes `scores.json`, and preserves
`manifest.json`, `verification.json` and `capture.jsonl` in its output. The report
retains the capture hash. Payment failure tests errors; suitable latency evidence
would be needed to validate timing scoring. Propagated error ties are retained.
The integration test exercises the verifier and actual report CLI with a generated
fixture; that passing test does not establish a live application experiment.

## Changed files and validation

Implementation: `diagnosis/score_v2.py`, `diagnosis/episodes.py`,
`diagnosis/__main__.py`, `diagnosis/reliability.py`, `diagnosis/fault_report.py`,
`experiments/diagnosis_method.py`, `experiments/rcaeval_eval.py`, `experiments/run.py`.
Configuration: `experiments/score_comparison.json`. Tests:
`diagnosis/tests/test_score_v2.py`, `experiments/tests/test_diagnosis_method.py`,
`reconstruction/tests/test_astronomy_smoke.py`. Documentation: this report,
`experiments/README.md`, and `reconstruction/smoke/README.md`.

See `experiments/reported/2026-11-19-score-v2-fixtures/` for saved fixture summaries
and reproduction configuration. These checks do not replace the unavailable full
benchmark, PostgreSQL integration, or live Docker experiment. V2 may still implicate
callers, has correlated latency/prevalence signals, and has uncalibrated weights and
confidence. Its memory/performance on full Train Ticket cases has not been measured.
Next: restore the 240-case dataset and run the frozen comparison; collect a verified
live payment capture; collect resource metrics before implementing MicroRCA.
