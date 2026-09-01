"""Tests for GrafanaMetricsTool (function-based, @tool decorated)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from infrastructure.evidence.metric_summary import summarize_prometheus_metrics
from integrations.grafana.tools import query_grafana_metrics
from integrations.grafana.tools.grafana_metrics_tool import (
    QueryGrafanaMetricsInput,
    _map_grafana_metrics,
)
from tests.synthetic.mock_grafana_backend.backend import FixtureGrafanaBackend
from tests.synthetic.rds_postgres.scenario_loader import SUITE_DIR, load_scenario
from tests.tools.conftest import BaseToolContract, mock_agent_state


class TestGrafanaMetricsToolContract(BaseToolContract):
    def get_tool_under_test(self):
        return query_grafana_metrics.__opensre_registered_tool__


def test_is_available_requires_grafana_creds() -> None:
    rt = query_grafana_metrics.__opensre_registered_tool__
    assert rt.is_available({"grafana": {"connection_verified": True}}) is True
    assert rt.is_available({"grafana": {}}) is False
    assert rt.is_available({}) is False


def test_extract_params_maps_fields() -> None:
    rt = query_grafana_metrics.__opensre_registered_tool__
    sources = mock_agent_state()
    params = rt.extract_params(sources)
    assert params["grafana_endpoint"] == "https://grafana.example.com"


def test_extract_params_does_not_guess_a_metric_name() -> None:
    """A fabricated default would query a metric nobody asked about; the seed
    path drops this tool instead (tests/integrations/grafana/test_seed_calls.py).
    """
    rt = query_grafana_metrics.__opensre_registered_tool__

    assert "metric_name" not in rt.extract_params(mock_agent_state())


def test_metric_name_examples_do_not_name_a_real_metric() -> None:
    """Concrete examples get copied verbatim when the agent has no better idea."""
    examples = QueryGrafanaMetricsInput.model_json_schema()["properties"]["metric_name"]["examples"]

    assert not any("pipeline_runs_total" in e or "http_requests_total" in e for e in examples)


def test_extract_params_includes_basic_auth_fields() -> None:
    rt = query_grafana_metrics.__opensre_registered_tool__
    sources = mock_agent_state({"grafana": {"username": "local-user", "password": "local-pass"}})
    params = rt.extract_params(sources)
    assert params["grafana_username"] == "local-user"
    assert params["grafana_password"] == "local-pass"


def test_injected_params_include_basic_auth_fields() -> None:
    rt = query_grafana_metrics.__opensre_registered_tool__
    assert "grafana_username" in rt.injected_params
    assert "grafana_password" in rt.injected_params


def test_run_with_backend() -> None:
    mock_backend = MagicMock()
    mock_backend.query_timeseries.return_value = {
        "data": {"result": [{"metric": {}, "values": [[1000, "42"]]}]}
    }
    result = query_grafana_metrics(metric_name="pipeline_runs_total", grafana_backend=mock_backend)
    assert result["available"] is True
    assert result["total_series"] == 1


def test_rds_storage_fixture_metrics_have_compact_summaries() -> None:
    fixture = load_scenario(SUITE_DIR / "003-storage-full")
    backend = FixtureGrafanaBackend(fixture)

    result = query_grafana_metrics(
        metric_name="pipeline_runs_total",
        service_name="rds-postgres-synthetic",
        grafana_backend=backend,
    )
    summaries = summarize_prometheus_metrics(result["metrics"])

    by_name = {summary["metric_name"]: summary for summary in summaries}
    assert "FreeStorageSpace" in by_name
    assert "WriteIOPS" in by_name
    assert "orders-prod" in by_name["FreeStorageSpace"]["summary"]
    assert "decreased" in by_name["FreeStorageSpace"]["trend"]
    assert "orders-prod" in by_name["WriteIOPS"]["summary"]
    assert "8100" in by_name["WriteIOPS"]["summary"]
    assert "peak_to_latest" in by_name["WriteIOPS"]["summary"]


def test_run_no_client() -> None:
    mock_client = MagicMock()
    mock_client.is_configured = False
    with patch(
        "integrations.grafana.tools._helpers._resolve_grafana_client", return_value=mock_client
    ):
        result = query_grafana_metrics(metric_name="cpu_usage", grafana_endpoint="http://grafana")
    assert result["available"] is False


def test_run_no_mimir_datasource() -> None:
    mock_client = MagicMock()
    mock_client.is_configured = True
    mock_client.mimir_datasource_uid = None
    with patch(
        "integrations.grafana.tools._helpers._resolve_grafana_client", return_value=mock_client
    ):
        result = query_grafana_metrics(metric_name="cpu_usage", grafana_endpoint="http://grafana")
    assert result["available"] is False
    assert "Mimir" in result["error"]


def test_run_happy_path() -> None:
    mock_client = MagicMock()
    mock_client.is_configured = True
    mock_client.mimir_datasource_uid = "mimir-uid"
    mock_client.account_id = "acc-1"
    mock_client.query_mimir.return_value = {
        "success": True,
        "metrics": [{"name": "pipeline_runs_total"}],
        "total_series": 1,
    }
    with patch(
        "integrations.grafana.tools._helpers._resolve_grafana_client", return_value=mock_client
    ):
        result = query_grafana_metrics(
            metric_name="pipeline_runs_total", grafana_endpoint="http://grafana"
        )
    assert result["available"] is True
    assert result["total_series"] == 1


def test_run_surfaces_the_truncation_hint_to_the_agent() -> None:
    """Capping series changes what the agent sees — it must be told, and told
    how to narrow, or it will reason over an arbitrary 50-series slice."""
    mock_client = MagicMock()
    mock_client.is_configured = True
    mock_client.mimir_datasource_uid = "mimir-uid"
    mock_client.account_id = "acc-1"
    mock_client.query_mimir.return_value = {
        "success": True,
        "metrics": [],
        "total_series": 4894,
        "truncated_series": 4844,
        "truncation_hint": "Showing 50 of 4894 series. Add label selectors...",
    }

    with patch(
        "integrations.grafana.tools._helpers._resolve_grafana_client", return_value=mock_client
    ):
        result = query_grafana_metrics(metric_name="up", grafana_endpoint="http://grafana")

    assert result["truncated_series"] == 4844
    assert "Add label selectors" in result["truncation_hint"]


def test_input_schema_exposes_the_time_window() -> None:
    """The model must be able to pin a window; without it only "now" is queryable."""
    schema = QueryGrafanaMetricsInput.model_json_schema()["properties"]

    assert set(schema) >= {"start", "end", "step"}


def test_run_forwards_the_time_window_to_the_client() -> None:
    mock_client = MagicMock()
    mock_client.is_configured = True
    mock_client.mimir_datasource_uid = "mimir-uid"
    mock_client.account_id = "acc-1"
    mock_client.query_mimir.return_value = {"success": True, "metrics": [], "total_series": 0}

    with patch(
        "integrations.grafana.tools._helpers._resolve_grafana_client", return_value=mock_client
    ):
        query_grafana_metrics(
            metric_name="cdb_cpu_use_rate",
            grafana_endpoint="http://grafana",
            start="2026-08-31T05:00:00Z",
            end="2026-08-31T05:30:00Z",
            step="15s",
        )

    assert mock_client.query_mimir.call_args.kwargs == {
        "service_name": None,
        "start": "2026-08-31T05:00:00Z",
        "end": "2026-08-31T05:30:00Z",
        "step": "15s",
    }


def _sources_with_window(window: dict[str, str]) -> dict[str, Any]:
    sources = mock_agent_state()
    sources["_meta"] = {"incident_window": window}
    return sources


_INCIDENT_WINDOW = {"since": "2026-08-31T05:00:00Z", "until": "2026-08-31T05:30:00Z"}


def test_extract_params_injects_the_window_as_a_single_value() -> None:
    """The runtime merges injected defaults key by key, so two scalar keys let a
    model-supplied ``start`` pair up with an injected ``end``. One value cannot
    be half-overridden.
    """
    rt = query_grafana_metrics.__opensre_registered_tool__

    params = rt.extract_params(_sources_with_window(_INCIDENT_WINDOW))

    assert params["shared_incident_window"] == {
        "start": "2026-08-31T05:00:00Z",
        "end": "2026-08-31T05:30:00Z",
    }
    assert "start" not in params
    assert "end" not in params


def test_extract_params_ignores_a_half_open_incident_window() -> None:
    """An injected default must be all-or-nothing."""
    rt = query_grafana_metrics.__opensre_registered_tool__

    params = rt.extract_params(_sources_with_window({"since": "2026-08-31T05:00:00Z"}))

    assert params["shared_incident_window"] is None


def test_extract_params_without_meta_leaves_the_window_unset() -> None:
    rt = query_grafana_metrics.__opensre_registered_tool__

    assert rt.extract_params(mock_agent_state())["shared_incident_window"] is None


def test_shared_window_is_protected_from_model_input() -> None:
    rt = query_grafana_metrics.__opensre_registered_tool__

    assert "shared_incident_window" in rt.injected_params


def _client_capturing_query() -> Any:
    client = MagicMock()
    client.is_configured = True
    client.mimir_datasource_uid = "mimir-uid"
    client.account_id = "acc-1"
    client.query_mimir.return_value = {"success": True, "metrics": [], "total_series": 0}
    return client


def test_incident_window_applies_when_the_model_gives_no_boundary() -> None:
    client = _client_capturing_query()

    with patch("integrations.grafana.tools._helpers._resolve_grafana_client", return_value=client):
        query_grafana_metrics(
            metric_name="cdb_cpu_use_rate",
            grafana_endpoint="http://grafana",
            shared_incident_window={
                "start": "2026-08-31T05:00:00Z",
                "end": "2026-08-31T05:30:00Z",
            },
        )

    kwargs = client.query_mimir.call_args.kwargs
    assert (kwargs["start"], kwargs["end"]) == ("2026-08-31T05:00:00Z", "2026-08-31T05:30:00Z")


def test_model_boundaries_win_over_the_incident_window() -> None:
    client = _client_capturing_query()

    with patch("integrations.grafana.tools._helpers._resolve_grafana_client", return_value=client):
        query_grafana_metrics(
            metric_name="cdb_cpu_use_rate",
            grafana_endpoint="http://grafana",
            start="2026-09-01T00:00:00Z",
            end="2026-09-01T01:00:00Z",
            shared_incident_window={
                "start": "2026-08-31T05:00:00Z",
                "end": "2026-08-31T05:30:00Z",
            },
        )

    kwargs = client.query_mimir.call_args.kwargs
    assert (kwargs["start"], kwargs["end"]) == ("2026-09-01T00:00:00Z", "2026-09-01T01:00:00Z")


def test_a_model_boundary_is_never_completed_from_the_incident_window() -> None:
    """The defect this pairing exists to prevent: a model ``start`` of 2026-09-01
    silently paired with an injected ``end`` of 2026-08-31 — an inverted window
    that reads as complete and so passes the half-open guard.
    """
    client = _client_capturing_query()

    with patch("integrations.grafana.tools._helpers._resolve_grafana_client", return_value=client):
        result = query_grafana_metrics(
            metric_name="cdb_cpu_use_rate",
            grafana_endpoint="http://grafana",
            start="2026-09-01T00:00:00Z",
            shared_incident_window={
                "start": "2026-08-31T05:00:00Z",
                "end": "2026-08-31T05:30:00Z",
            },
        )

    assert result["available"] is False
    client.query_mimir.assert_not_called()


def test_backend_path_rejects_a_half_open_window() -> None:
    """The HTTP path refuses this input; a fixture-backed run must not quietly
    answer a different, windowless question instead.
    """
    backend = MagicMock()

    result = query_grafana_metrics(
        metric_name="cdb_cpu_use_rate",
        grafana_backend=backend,
        start="2026-08-31T05:00:00Z",
    )

    assert result["available"] is False
    backend.query_timeseries.assert_not_called()


def test_backend_path_forwards_the_time_window() -> None:
    mock_backend = MagicMock()
    mock_backend.query_timeseries.return_value = {"data": {"result": []}}

    query_grafana_metrics(
        metric_name="cdb_cpu_use_rate",
        grafana_backend=mock_backend,
        start="2026-08-31T05:00:00Z",
        end="2026-08-31T05:30:00Z",
    )

    assert mock_backend.query_timeseries.call_args.kwargs == {
        "query": "cdb_cpu_use_rate",
        "start": "2026-08-31T05:00:00Z",
        "end": "2026-08-31T05:30:00Z",
    }


def _curve(name: str, values: list[list[object]]) -> dict[str, Any]:
    return {"metric": {"__name__": name, "instanceid": "cdbro-ov6a6jkc"}, "values": values}


def _run_mapper(output: dict[str, Any], evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    evidence = {} if evidence is None else evidence
    _map_grafana_metrics(evidence, output, {})
    return evidence


def test_citation_reports_total_series_not_the_capped_page() -> None:
    """The cap keeps the payload small; the citation must still say how many
    series exist, or the report understates the blast radius it is describing.
    """
    evidence = _run_mapper(
        {
            "metric_name": "up",
            "metrics": [_curve("up", [[1, "1"]]) for _ in range(50)],
            "total_series": 4894,
            "truncated_series": 4844,
        }
    )

    summary = evidence["catalog_entries"][0]["summary"]
    assert "4894 series" in summary
    assert "showing 50" in summary


def test_citation_names_the_window_it_was_measured_over() -> None:
    """Every claim in the report is anchored to the incident window; a citation
    without one cannot be checked or reproduced.
    """
    evidence = _run_mapper(
        {
            "metric_name": "cdb_cpu_use_rate",
            "metrics": [_curve("cdb_cpu_use_rate", [[1756616400, "84.75"]])],
            "total_series": 1,
            "start": "2026-08-31T05:00:00Z",
            "end": "2026-08-31T05:30:00Z",
        }
    )

    summary = evidence["catalog_entries"][0]["summary"]
    assert "2026-08-31T05:00:00Z" in summary
    assert "2026-08-31T05:30:00Z" in summary


def test_citation_carries_the_shape_of_the_curve() -> None:
    """A bare series count cannot support "CPU stayed near 100%"."""
    evidence = _run_mapper(
        {
            "metric_name": "cdb_cpu_use_rate",
            "metrics": [
                _curve(
                    "cdb_cpu_use_rate",
                    [[1756616400, "84.75"], [1756616415, "99.89"], [1756616430, "95.28"]],
                )
            ],
            "total_series": 1,
        }
    )

    summary = evidence["catalog_entries"][0]["summary"]
    assert "84.75" in summary  # min
    assert "99.89" in summary  # max


def test_each_metric_query_is_cited_separately() -> None:
    """One investigation queries a dozen metrics; collapsing them onto a single
    citation drops the evidence the correlation argument rests on.
    """
    evidence: dict[str, Any] = {}
    for name in ("cdb_cpu_use_rate", "cdb_qps_total", "cdb_threads_running_total"):
        _run_mapper(
            {"metric_name": name, "metrics": [_curve(name, [[1, "1"]])], "total_series": 1},
            evidence,
        )

    keys = [entry.get("key") for entry in evidence["catalog_entries"]]
    assert keys == ["cdb_cpu_use_rate", "cdb_qps_total", "cdb_threads_running_total"]


def test_the_same_metric_over_two_windows_is_two_citations() -> None:
    """The loop is told to re-run a tool with a *different* time window, so an
    incident-vs-baseline comparison is expected. Keying on the metric alone
    keeps the first citation while the results map keeps the last, leaving the
    report disagreeing with itself.
    """
    evidence: dict[str, Any] = {}
    for start, end, value in (
        ("2026-08-31T14:30:00Z", "2026-08-31T14:45:00Z", "99.89"),
        ("2026-08-31T13:00:00Z", "2026-08-31T13:15:00Z", "4.2"),
    ):
        _run_mapper(
            {
                "metric_name": "cdb_cpu_use_rate",
                "metrics": [_curve("cdb_cpu_use_rate", [[1, value]])],
                "total_series": 1,
                "start": start,
                "end": end,
            },
            evidence,
        )

    assert len(evidence["catalog_entries"]) == 2
    assert len(evidence["grafana_metric_results"]) == 2


def test_citation_extrema_span_every_returned_series() -> None:
    """Prometheus does not order series, so the first one is arbitrary. Quoting
    its range as the metric's range can be wrong by orders of magnitude.
    """
    evidence = _run_mapper(
        {
            "metric_name": "cdb_cpu_use_rate",
            "metrics": [
                _curve("cdb_cpu_use_rate", [[1, "1"], [2, "1.5"]]),
                _curve("cdb_cpu_use_rate", [[1, "50"], [2, "100"]]),
            ],
            "total_series": 2,
        }
    )

    summary = evidence["catalog_entries"][0]["summary"]
    assert "min 1, max 100" in summary


def test_backend_results_obey_the_same_limits_as_the_http_path() -> None:
    """A fixture or CSV replay can hold a whole day of samples. Returning them
    unbounded puts far more into the transcript than the equivalent Mimir call,
    where the context budget then evicts it without telling anyone.
    """
    backend = MagicMock()
    backend.query_timeseries.return_value = {
        "data": {
            "result": [
                {"metric": {"i": str(i)}, "values": [[t, "1"] for t in range(3000)]}
                for i in range(200)
            ]
        }
    }

    result = query_grafana_metrics(
        metric_name="cdb_cpu_use_rate",
        grafana_backend=backend,
        start="2026-08-31T05:00:00Z",
        end="2026-08-31T05:30:00Z",
    )

    assert len(result["metrics"]) == 50
    assert result["total_series"] == 200
    assert result["truncated_series"] == 150
    assert "Add label selectors" in result["truncation_hint"]
    assert all(len(series["values"]) <= 200 for series in result["metrics"])


def test_backend_results_cite_the_window_they_were_queried_for() -> None:
    """A citation built from the backend path must name the window like the
    HTTP path does, or fixture-backed evidence looks unbounded.
    """
    backend = MagicMock()
    backend.query_timeseries.return_value = {"data": {"result": []}}

    result = query_grafana_metrics(
        metric_name="cdb_cpu_use_rate",
        grafana_backend=backend,
        start="2026-08-31T05:00:00Z",
        end="2026-08-31T05:30:00Z",
    )

    assert result["start"] == "2026-08-31T05:00:00Z"
    assert result["end"] == "2026-08-31T05:30:00Z"


def test_run_records_the_resolved_window_on_the_output() -> None:
    """The mapper cites the window that was actually queried, including one
    injected from the incident window rather than named by the model.
    """
    client = _client_capturing_query()

    with patch("integrations.grafana.tools._helpers._resolve_grafana_client", return_value=client):
        result = query_grafana_metrics(
            metric_name="cdb_cpu_use_rate",
            grafana_endpoint="http://grafana",
            shared_incident_window={
                "start": "2026-08-31T05:00:00Z",
                "end": "2026-08-31T05:30:00Z",
            },
        )

    assert result["start"] == "2026-08-31T05:00:00Z"
    assert result["end"] == "2026-08-31T05:30:00Z"
