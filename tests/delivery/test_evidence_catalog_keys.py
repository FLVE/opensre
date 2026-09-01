"""Catalog identity for entries recorded by per-tool evidence mappers."""

from __future__ import annotations

from typing import Any

from core.domain.types.evidence import record_evidence_entry
from tools.investigation.reporting.context.evidence_catalog import _add_mapped_entries


def _catalog(evidence: dict[str, Any], claimed: dict[str, str] | None = None):
    catalog: dict[str, dict] = {}
    source_to_id: dict[str, str] = dict(claimed or {})
    _add_mapped_entries(evidence, catalog, source_to_id)
    return catalog, source_to_id


def test_keyed_entries_from_one_source_are_cited_separately() -> None:
    """An investigation queries a dozen metrics and its argument rests on all
    of them; collapsing the source to one citation drops the rest.
    """
    evidence: dict[str, Any] = {}
    for metric in ("cdb_cpu_use_rate", "cdb_qps_total", "cdb_threads_running_total"):
        record_evidence_entry(
            evidence,
            source="grafana_metrics",
            key=metric,
            label="Grafana Metrics",
            summary=f"{metric}, 1 series",
        )

    catalog, source_to_id = _catalog(evidence)

    assert len(catalog) == 3
    summaries = sorted(entry["summary"] for entry in catalog.values())
    assert summaries == [
        "cdb_cpu_use_rate, 1 series",
        "cdb_qps_total, 1 series",
        "cdb_threads_running_total, 1 series",
    ]
    # Claims reference a source, not a metric, so the index keeps one id.
    assert source_to_id["grafana_metrics"] in catalog


def test_repeating_the_same_key_still_collapses() -> None:
    """Re-running an identical query is not new evidence."""
    evidence: dict[str, Any] = {}
    for _ in range(3):
        record_evidence_entry(
            evidence, source="grafana_metrics", key="up", label="Grafana Metrics", summary="up"
        )

    catalog, _ = _catalog(evidence)

    assert len(catalog) == 1


def test_unkeyed_entries_still_collapse_to_one_per_source() -> None:
    """Tools yielding a single body of evidence keep their existing behaviour."""
    evidence: dict[str, Any] = {}
    for i in range(3):
        record_evidence_entry(
            evidence, source="betterstack_logs", label="Better Stack", summary=f"batch {i}"
        )

    catalog, _ = _catalog(evidence)

    assert len(catalog) == 1
    assert next(iter(catalog.values()))["summary"] == "batch 0"


def test_a_bespoke_reader_still_wins_over_mapped_entries() -> None:
    """The hand-written catalog reader owns its source, keys included."""
    evidence: dict[str, Any] = {}
    record_evidence_entry(
        evidence, source="grafana_metrics", key="up", label="Grafana Metrics", summary="up"
    )

    catalog, _ = _catalog(evidence, claimed={"grafana_metrics": "evidence/bespoke/grafana"})

    assert catalog == {}
