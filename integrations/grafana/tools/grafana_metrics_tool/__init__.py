"""Grafana Mimir metrics query tool."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

import integrations.grafana.tools._helpers as grafana_helpers
from core.domain.types.evidence import record_evidence_entry
from core.tool import EvidenceType, SideEffectLevel
from core.tool_framework import tool
from core.tool_framework.utils import tool_unavailable
from infrastructure.evidence.metric_summary import summarize_prometheus_metrics
from integrations.opensre.grafana_backend_queries import query_metrics_from_backend

_GRAFANA_RUNTIME_PARAMS = (*grafana_helpers.GRAFANA_RUNTIME_PARAMS, "shared_incident_window")


class QueryGrafanaMetricsInput(BaseModel):
    metric_name: str = Field(
        description="Grafana Mimir metric query expression to execute.",
        examples=["<metric_name>", "sum(rate(<metric_name>[5m]))"],
    )
    service_name: str | None = Field(
        default=None,
        description=(
            "Optional service filter. Only valid for a bare metric name; for an "
            "expression, put the label selector inside the expression instead."
        ),
    )
    start: str | None = Field(
        default=None,
        description=(
            "Window start (RFC 3339 or Unix seconds). Set with `end` to get a time "
            "series instead of a single current value. Defaults to the incident window."
        ),
        examples=["2026-08-31T05:00:00Z"],
    )
    end: str | None = Field(
        default=None,
        description="Window end (RFC 3339 or Unix seconds). Required with `start`.",
        examples=["2026-08-31T05:30:00Z"],
    )
    step: str | None = Field(
        default=None,
        description=(
            "Resolution between points, e.g. `15s` or `5m`. Chosen automatically when "
            "omitted; raised if it would return too many points."
        ),
    )


class QueryGrafanaMetricsOutput(BaseModel):
    source: str = Field(description="Evidence source label.")
    available: bool = Field(description="Whether Grafana query execution succeeded.")
    metric_name: str = Field(description="Metric query string that was executed.")
    service_name: str | None = Field(default=None, description="Service filter used for the query.")
    start: str | None = Field(default=None, description="Window start actually queried.")
    end: str | None = Field(default=None, description="Window end actually queried.")
    total_series: int = Field(default=0, description="Number of timeseries returned.")
    metrics: list[dict[str, Any]] = Field(default_factory=list, description="Raw metrics payload.")
    truncated_series: int | None = Field(
        default=None,
        description="Series dropped by the result cap; narrow the query when set.",
    )
    truncation_hint: str | None = Field(
        default=None,
        description="How to narrow the query when the result cap dropped series.",
    )
    error: str | None = Field(default=None, description="Error details when query fails.")
    account_id: int | None = Field(default=None, description="Grafana account id when available.")


def _query_grafana_metrics_extract_params(sources: dict[str, dict]) -> dict[str, Any]:
    grafana = grafana_helpers._grafana_source(sources)
    # ``_meta`` carries investigation-level context shared across tools. It
    # supplies the default window; explicit model input still wins because
    # neither key is listed in ``injected_params``.
    raw_meta = sources.get("_meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    window = meta.get("incident_window")
    window = window if isinstance(window, dict) else {}
    start = window.get("since") or window.get("start")
    end = window.get("until") or window.get("end")
    # One value, not two keys: the runtime merges injected defaults key by key,
    # so a model-supplied ``start`` would otherwise pair with an injected
    # ``end`` into a window neither side asked for. Same shape as
    # git_deploy_timeline_tool's ``shared_incident_window``.
    # No metric_name: which metric to fetch depends on the alert, so guessing
    # one here only produced seed calls for a metric nobody asked about.
    # build_seed_calls now drops this tool's seed instead; the model supplies
    # metric_name on its own turns.
    return {
        "service_name": grafana.get("service_name"),
        "shared_incident_window": {"start": start, "end": end} if start and end else None,
        "grafana_backend": grafana.get("_backend"),
        **grafana_helpers._grafana_creds(grafana),
    }


def _query_grafana_metrics_available(sources: dict[str, dict]) -> bool:
    return grafana_helpers._grafana_available(sources)


def _citation_summary(output: dict[str, Any], metric_name: str) -> str:
    """Describe a metric result well enough that a reader can re-run it.

    A bare series count cannot support a claim about how a metric moved, and
    after truncation it is not even the real count.
    """
    metrics = output.get("metrics") or []
    total = output.get("total_series", len(metrics))
    parts = [p for p in [metric_name or None] if p]

    series = f"{total} series"
    if output.get("truncated_series"):
        series += f" (showing {len(metrics)})"
    parts.append(series)

    start, end = output.get("start"), output.get("end")
    if start and end:
        parts.append(f"{start} → {end}")

    # Across every returned series: Prometheus does not order them, so quoting
    # the first one's range as the metric's range can be off by orders of
    # magnitude when a later series carries the spike.
    summaries = summarize_prometheus_metrics(metrics)
    lows = [s["min"] for s in summaries if s.get("min") is not None]
    highs = [s["max"] for s in summaries if s.get("max") is not None]
    if lows and highs:
        parts.append(f"min {min(lows):g}, max {max(highs):g}")

    return ", ".join(parts)


def _result_key(output: dict[str, Any], metric_name: str) -> str:
    """Identify one query, not one metric.

    The agent is told to re-run a tool with different arguments — another
    service, another window — so keying on the metric alone makes an
    incident-vs-baseline pair overwrite itself.
    """
    dimensions = (
        metric_name,
        output.get("service_name"),
        output.get("start"),
        output.get("end"),
    )
    return "|".join(str(part) for part in dimensions if part)


def _map_grafana_metrics(
    evidence: dict[str, Any], output: dict[str, Any], tool_input: dict[str, Any]
) -> None:
    metric_name = str(output.get("metric_name") or tool_input.get("metric_name") or "")
    # The results map and the citation share one key, or the report cites one
    # query while the map holds another.
    result_key = _result_key(output, metric_name)
    metric_results = evidence.setdefault("grafana_metric_results", {})
    if isinstance(metric_results, dict) and result_key:
        metric_results[result_key] = output
    metrics = output.get("metrics", [])
    evidence["grafana_metrics"] = metrics
    if metrics:
        record_evidence_entry(
            evidence,
            source="grafana_metrics",
            # One entry per query: an investigation runs a dozen and the
            # correlation argument rests on all of them, not the first.
            key=result_key,
            label="Grafana Metrics",
            summary=_citation_summary(output, metric_name),
        )


@tool(
    name="query_grafana_metrics",
    display_name="Grafana Mimir",
    source="grafana",
    evidence_mapper=_map_grafana_metrics,
    description="Query Grafana Cloud Mimir for pipeline metrics.",
    use_cases=[
        "Checking pipeline throughput and error rate metrics",
        "Reviewing resource utilisation trends over time",
        "Correlating metric anomalies with alert triggers",
    ],
    requires=["metric_name"],
    source_id="grafana_mimir",
    evidence_type=EvidenceType.METRICS,
    side_effect_level=SideEffectLevel.READ_ONLY,
    examples=[
        "Query `pipeline_runs_total` to verify throughput drops.",
        "Query HTTP error rate metric with a `service_name` filter.",
    ],
    anti_examples=["Use this tool for pod logs or deployment status."],
    input_model=QueryGrafanaMetricsInput,
    output_model=QueryGrafanaMetricsOutput,
    injected_params=_GRAFANA_RUNTIME_PARAMS,
    is_available=_query_grafana_metrics_available,
    extract_params=_query_grafana_metrics_extract_params,
)
def query_grafana_metrics(
    metric_name: str,
    service_name: str | None = None,
    start: str | None = None,
    end: str | None = None,
    step: str | None = None,
    grafana_endpoint: str | None = None,
    grafana_api_key: str | None = None,
    grafana_username: str = "",
    grafana_password: str = "",
    grafana_verify_ssl: bool = True,
    grafana_ca_bundle: str = "",
    grafana_backend: Any = None,
    shared_incident_window: dict[str, Any] | None = None,
    **_kwargs: Any,
) -> dict:
    """Query Grafana Cloud Mimir for pipeline metrics.

    The incident window fills in only when the caller supplied neither
    boundary, so a model-chosen ``start`` is never completed by an injected
    ``end``. A half-open window is refused on every path.
    """
    if start is None and end is None and shared_incident_window:
        start = shared_incident_window.get("start")
        end = shared_incident_window.get("end")

    if bool(start) != bool(end):
        return tool_unavailable(
            "grafana_mimir",
            "a time window needs both start and end",
            metrics=[],
        )

    if grafana_backend is not None:
        return query_metrics_from_backend(
            grafana_backend,
            metric_name=metric_name,
            service_name=service_name,
            start=start,
            end=end,
        )

    client = grafana_helpers._resolve_grafana_client(
        grafana_endpoint,
        grafana_api_key,
        grafana_username,
        grafana_password,
        grafana_verify_ssl,
        grafana_ca_bundle,
    )
    if not client or not client.is_configured:
        return tool_unavailable("grafana_mimir", "Grafana integration not configured", metrics=[])
    if not client.mimir_datasource_uid:
        return tool_unavailable("grafana_mimir", "Mimir datasource not found", metrics=[])

    result = client.query_mimir(
        metric_name, service_name=service_name, start=start, end=end, step=step
    )
    if not result.get("success"):
        return tool_unavailable("grafana_mimir", result.get("error", "Unknown error"), metrics=[])

    output = {
        "source": "grafana_mimir",
        "available": True,
        "metrics": result.get("metrics", []),
        "total_series": result.get("total_series", 0),
        "metric_name": metric_name,
        "service_name": service_name,
        # The window actually queried, which may have come from the incident
        # window rather than the model — the citation has to name it either way.
        "start": start,
        "end": end,
        "account_id": client.account_id,
    }
    if result.get("truncated_series"):
        output["truncated_series"] = result["truncated_series"]
        output["truncation_hint"] = result.get("truncation_hint")
    return output


__all__ = [
    "QueryGrafanaMetricsInput",
    "QueryGrafanaMetricsOutput",
    "query_grafana_metrics",
]
