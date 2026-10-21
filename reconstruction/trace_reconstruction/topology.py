"""Pure trace grouping and topology reconstruction.

This module deliberately does not calculate self time or a critical path.  It
only establishes the observed parent topology and the evidence needed to say
whether that topology is connected, incomplete, invalid, or unknown.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, MutableMapping, Optional


class IngestDisposition(str, Enum):
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"


def _identity(record: Mapping) -> tuple[str, str, str]:
    return str(record["run_id"]), str(record["trace_id"]), str(record["span_id"])


def semantic_content(record: Mapping) -> dict:
    """Return content used for retry deduplication.

    Collector reception time and raw archive location may legitimately change
    on a retry.  Every other field remains part of semantic equality, matching
    ``trace_v1.canonical_content`` in PostgreSQL.
    """
    content = copy.deepcopy(dict(record))
    content.pop("received_at_unix_ns", None)
    source = content.get("source")
    if isinstance(source, MutableMapping):
        source.pop("raw_reference", None)
    return content


def _stable_json(value: Mapping) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def span_sort_key(record: Mapping) -> tuple[int, int, str]:
    """Measured/derived spans first by start time, then stable span identity."""
    start = record.get("start_time_ns")
    return (start is None, int(start) if start is not None else 0, str(record["span_id"]))


@dataclass
class TraceGroup:
    """First-observation-wins grouping for one ``(run_id, trace_id)``."""

    run_id: str
    trace_id: str
    records: dict[str, dict] = field(default_factory=dict)
    duplicate_count: int = 0
    conflict_span_ids: set[str] = field(default_factory=set)

    def add(self, record: Mapping) -> IngestDisposition:
        run_id, trace_id, span_id = _identity(record)
        if (run_id, trace_id) != (self.run_id, self.trace_id):
            raise ValueError("record belongs to a different run or trace")
        accepted = self.records.get(span_id)
        if accepted is None:
            self.records[span_id] = copy.deepcopy(dict(record))
            return IngestDisposition.INSERTED
        if _stable_json(semantic_content(accepted)) == _stable_json(semantic_content(record)):
            self.duplicate_count += 1
            return IngestDisposition.DUPLICATE
        self.conflict_span_ids.add(span_id)
        return IngestDisposition.CONFLICT

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflict_span_ids)

    def reconstruct(self) -> "ReconstructedTrace":
        return reconstruct_trace(
            self.records.values(),
            duplicate_count=self.duplicate_count,
            conflict_span_ids=self.conflict_span_ids,
        )


@dataclass(frozen=True)
class ReconstructedTrace:
    run_id: str
    trace_id: str
    topology: str
    root_span_id: Optional[str]
    spans: tuple[dict, ...]
    parents: dict[str, Optional[str]]
    children: dict[str, tuple[str, ...]]
    quality: dict

    def span_features(self) -> dict:
        """Serializable flat topology stored with the trace analysis."""
        return {
            span["span_id"]: {
                "parent_span_id": self.parents[span["span_id"]],
                "children": list(self.children[span["span_id"]]),
            }
            for span in self.spans
        }


def group_records(records: Iterable[Mapping]) -> dict[tuple[str, str], TraceGroup]:
    groups: dict[tuple[str, str], TraceGroup] = {}
    for record in records:
        run_id, trace_id, _ = _identity(record)
        group = groups.setdefault((run_id, trace_id), TraceGroup(run_id, trace_id))
        group.add(record)
    return groups


def _has_cycle(nodes: Mapping[str, Mapping]) -> bool:
    done: set[str] = set()
    for start in nodes:
        path: set[str] = set()
        current: Optional[str] = start
        while current in nodes and current not in done:
            if current in path:
                return True
            path.add(current)
            current = nodes[current].get("parent_span_id")
        done.update(path)
    return False


def reconstruct_trace(
    records: Iterable[Mapping],
    *,
    duplicate_count: Optional[int] = None,
    conflict_span_ids: Iterable[str] = (),
    has_conflicts: bool = False,
) -> ReconstructedTrace:
    materialized = [copy.deepcopy(dict(record)) for record in records]
    if not materialized:
        raise ValueError("cannot reconstruct an empty trace")

    trace_keys = {(str(r["run_id"]), str(r["trace_id"])) for r in materialized}
    if len(trace_keys) != 1:
        raise ValueError("reconstruct_trace expects exactly one run and trace")
    identities = [str(r["span_id"]) for r in materialized]
    if len(set(identities)) != len(identities):
        raise ValueError("deduplicate records before reconstruction")

    run_id, trace_id = next(iter(trace_keys))
    nodes = {str(record["span_id"]): record for record in materialized}
    ordered = tuple(sorted(materialized, key=span_sort_key))
    roots = [str(r["span_id"]) for r in ordered if r.get("parent_relation") == "ROOT"]
    missing = sorted({
        str(r["parent_span_id"])
        for r in ordered
        if r.get("parent_span_id") is not None and str(r["parent_span_id"]) not in nodes
    })
    unknown_parent_count = sum(r.get("parent_relation") == "UNKNOWN" for r in ordered)
    inferred_parent_count = sum(r.get("parent_relation") == "INFERRED" for r in ordered)
    cycle = _has_cycle(nodes)

    if cycle or len(roots) > 1:
        topology = "INVALID"
    elif unknown_parent_count:
        topology = "UNKNOWN"
    elif missing or len(roots) != 1:
        topology = "INCOMPLETE"
    else:
        topology = "CONNECTED"

    children_lists: dict[str, list[str]] = {span_id: [] for span_id in nodes}
    for child in ordered:
        parent_id = child.get("parent_span_id")
        if parent_id in nodes:
            children_lists[str(parent_id)].append(str(child["span_id"]))
    for parent_id, child_ids in children_lists.items():
        child_ids.sort(key=lambda child_id: span_sort_key(nodes[child_id]))

    out_of_bounds = 0
    for child in ordered:
        parent = nodes.get(str(child.get("parent_span_id")))
        if parent is None:
            continue
        if child.get("start_time_ns") is None or parent.get("start_time_ns") is None:
            continue
        if child.get("time_domain") != parent.get("time_domain"):
            continue
        if (int(child["start_time_ns"]) < int(parent["start_time_ns"]) or
                int(child["end_time_ns"]) > int(parent["end_time_ns"])):
            out_of_bounds += 1

    observed_status_count = sum(r.get("status", {}).get("code") in {"OK", "ERROR"} for r in ordered)
    error_span_count = sum(r.get("status", {}).get("code") == "ERROR" for r in ordered)
    conflicts = sorted(set(map(str, conflict_span_ids)))
    unavailable_reasons = {
        "self_time": "deferred_to_sprint_4",
        "critical_path": "deferred_to_sprint_4",
    }
    if has_conflicts and not conflicts:
        unavailable_reasons["conflict_span_ids"] = "conflict_payloads_are_retained_outside_trace_state"
    if duplicate_count is None:
        unavailable_reasons["duplicate_count"] = "operational_duplicate_count_not_persisted"
    quality = {
        "span_count": len(ordered),
        "root_count": len(roots),
        "missing_parent_ids": missing,
        "unknown_parent_count": unknown_parent_count,
        "inferred_parent_count": inferred_parent_count,
        "cycle": cycle,
        "out_of_bounds_child_count": out_of_bounds,
        "duplicate_count": duplicate_count,
        "duplicate_count_available": duplicate_count is not None,
        "has_conflicts": has_conflicts or bool(conflicts),
        "conflict_span_ids": conflicts,
        "error_span_count": error_span_count,
        "observed_status_count": observed_status_count,
        "self_time_ready": False,
        "critical_path_ready": False,
        "unavailable_reasons": unavailable_reasons,
    }
    return ReconstructedTrace(
        run_id=run_id,
        trace_id=trace_id,
        topology=topology,
        root_span_id=roots[0] if len(roots) == 1 else None,
        spans=ordered,
        parents={str(r["span_id"]): r.get("parent_span_id") for r in ordered},
        children={key: tuple(value) for key, value in children_lists.items()},
        quality=quality,
    )
