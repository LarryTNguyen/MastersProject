"""Quiet-window settlement and topology publication."""
from __future__ import annotations

import time
from dataclasses import dataclass

from .store import StaleTraceError
from .topology import reconstruct_trace


@dataclass
class WorkerStats:
    settled: int = 0
    analyzed: int = 0
    stale: int = 0


class ReconstructionWorker:
    def __init__(self, store, *, algorithm_version: str = "topology-v1", configuration=None):
        self.store = store
        self.algorithm_version = algorithm_version
        self.configuration = configuration or {}

    def _analyze_candidate(self, candidate) -> bool:
        records, revision, has_conflicts = self.store.fetch_trace(candidate.run_id, candidate.trace_id)
        if revision != candidate.revision:
            return False
        trace = reconstruct_trace(records, has_conflicts=has_conflicts)
        try:
            self.store.publish_topology(
                trace,
                revision,
                algorithm_version=self.algorithm_version,
                configuration=self.configuration,
            )
            return True
        except StaleTraceError:
            return False

    def poll_once(self, limit: int = 100) -> WorkerStats:
        stats = WorkerStats()
        for candidate in self.store.due_traces(limit=limit):
            if self.store.settle(candidate):
                stats.settled += 1
            else:
                stats.stale += 1
        for candidate in self.store.pending_analyses(self.algorithm_version, limit=limit):
            if self._analyze_candidate(candidate):
                stats.analyzed += 1
            else:
                stats.stale += 1
        return stats

    def finish_run(self, run_id: str) -> WorkerStats:
        self.store.end_run(run_id)
        stats = WorkerStats()
        for candidate in self.store.open_traces_for_run(run_id):
            if self.store.settle(candidate):
                stats.settled += 1
            else:
                stats.stale += 1
        for candidate in self.store.pending_analyses(self.algorithm_version, limit=100000):
            if candidate.run_id != run_id:
                continue
            if self._analyze_candidate(candidate):
                stats.analyzed += 1
            else:
                stats.stale += 1
        return stats

    def run_forever(self, *, interval_seconds: float = 1.0, limit: int = 100) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        while True:
            self.poll_once(limit=limit)
            time.sleep(interval_seconds)
