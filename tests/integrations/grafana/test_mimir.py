"""Unit tests for Grafana Mimir metrics query mixin."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from integrations.grafana.mimir import MimirMixin


class DummyMimirClient(MimirMixin):
    """Dummy client to isolate and test the MimirMixin without network calls."""

    def __init__(self, is_configured: bool = True) -> None:
        super().__init__()

        # Fake properties that MimirMixin expects from GrafanaClientBase
        self.is_configured = is_configured
        self.account_id = "test_acc_123"
        self.mimir_datasource_uid = "mimir_uid_456"

        # Mock the internal base methods to intercept network calls
        self._build_datasource_url = MagicMock(return_value="https://fake-grafana.com/api/v1/query")
        self._make_get_request = MagicMock()


def test_query_mimir_plain_metric():
    """Test query construction for a plain metric without a service filter."""
    client = DummyMimirClient()
    client._make_get_request.return_value = {"data": {"result": []}}

    client.query_mimir("cpu_usage_total")

    # Verify PromQL exact match (Requirement: Mimir query construction is protected)
    client._make_get_request.assert_called_once_with(
        "https://fake-grafana.com/api/v1/query", params={"query": "cpu_usage_total"}
    )


def test_query_mimir_service_filtered():
    """Test query construction when a service_name filter is provided."""
    client = DummyMimirClient()
    client._make_get_request.return_value = {"data": {"result": []}}

    client.query_mimir("cpu_usage_total", service_name="backend-api")

    # Verify PromQL bracket injection (Requirement: Service filtering is tested)
    client._make_get_request.assert_called_once_with(
        "https://fake-grafana.com/api/v1/query",
        params={"query": 'cpu_usage_total{service_name="backend-api"}'},
    )


def test_query_mimir_result_normalization():
    """Test that raw Grafana JSON is normalized into the expected clean dictionary."""
    client = DummyMimirClient()

    # Simulate a messy response from the real Grafana API
    fake_api_response = {
        "data": {
            "result": [
                {
                    "metric": {"__name__": "cpu_usage_total", "instance": "server-1"},
                    "value": [1670000000, "95.5"],
                }
            ]
        }
    }
    client._make_get_request.return_value = fake_api_response

    result = client.query_mimir("cpu_usage_total")

    # Requirement: Result-series normalization is covered
    assert result["success"] is True
    assert result["total_series"] == 1
    assert result["query"] == "cpu_usage_total"
    assert len(result["metrics"]) == 1
    assert result["account_id"] == "test_acc_123"

    metric_data = result["metrics"][0]
    assert metric_data["metric"]["instance"] == "server-1"
    assert metric_data["value"][1] == "95.5"


def test_query_mimir_not_configured():
    """Test the early exit path when the client is not configured."""
    client = DummyMimirClient(is_configured=False)

    result = client.query_mimir("cpu_usage_total")

    # Requirement: Not-configured cases
    assert result["success"] is False
    assert "not configured" in result["error"]
    client._make_get_request.assert_not_called()


def test_query_mimir_exception_handling():
    """Test that network exceptions are caught and wrapped in a safe error envelope."""
    client = DummyMimirClient()

    client._make_get_request.side_effect = Exception("Network timeout")

    result = client.query_mimir("cpu_usage_total")

    # Requirement: Exception cases / error envelope
    assert result["success"] is False
    assert "Network timeout" in result["error"]
    assert result["metrics"] == []


def test_query_mimir_http_exception_handling():
    client = DummyMimirClient()

    # 1. Create a fake HTTP response object
    mock_response = MagicMock()
    mock_response.status_code = 502
    mock_response.text = "Bad Gateway: Mimir database is unreachable"

    # 2. Create a generic exception, but attach our fake response to it
    mock_exception = Exception("HTTP Error")
    mock_exception.response = mock_response

    # 3. Force the mock client to crash using our custom exception
    client._make_get_request.side_effect = mock_exception

    result = client.query_mimir("cpu_usage_total")

    # 4. Verify it hit lines 69-71 and correctly formatted the error
    assert result["success"] is False
    assert result["error"] == "Mimir query failed: 502"
    assert result["response"] == "Bad Gateway: Mimir database is unreachable"
    assert result["metrics"] == []


def test_query_mimir_rejects_service_filter_on_expression_query():
    """Appending a selector to an expression yields invalid PromQL — refuse instead.

    ``sum(rate(x[5m])){service_name="api"}`` parses nowhere; the caller must put
    the label inside the expression.
    """
    client = DummyMimirClient()

    result = client.query_mimir("sum(rate(http_requests_total[5m]))", service_name="backend-api")

    assert result["success"] is False
    assert "service_name" in result["error"]
    assert result["metrics"] == []
    client._make_get_request.assert_not_called()


def test_query_mimir_with_window_uses_query_range_endpoint():
    """A time window must reach Mimir as a range query, not an instant one.

    Instant queries return one point per series, which cannot support the
    cross-metric time correlation an investigation needs.
    """
    client = DummyMimirClient()
    client._make_get_request.return_value = {"data": {"result": []}}

    client.query_mimir(
        "cdb_cpu_use_rate",
        start="2026-08-31T05:00:00Z",
        end="2026-08-31T05:30:00Z",
        step="15s",
    )

    client._build_datasource_url.assert_called_once_with("mimir_uid_456", "/api/v1/query_range")
    _url, kwargs = client._make_get_request.call_args
    assert kwargs["params"] == {
        "query": "cdb_cpu_use_rate",
        "start": "2026-08-31T05:00:00Z",
        "end": "2026-08-31T05:30:00Z",
        "step": "15s",
    }


def test_query_mimir_range_normalizes_values_series():
    """Range responses carry ``values`` (a curve), not the instant ``value``."""
    client = DummyMimirClient()
    client._make_get_request.return_value = {
        "data": {
            "result": [
                {
                    "metric": {"__name__": "cdb_cpu_use_rate", "instanceid": "cdbro-ov6a6jkc"},
                    "values": [[1756616400, "12.5"], [1756616415, "88.1"]],
                }
            ]
        }
    }

    result = client.query_mimir(
        "cdb_cpu_use_rate", start="2026-08-31T05:00:00Z", end="2026-08-31T05:30:00Z"
    )

    assert result["success"] is True
    assert result["total_series"] == 1
    assert result["metrics"][0]["values"] == [[1756616400, "12.5"], [1756616415, "88.1"]]
    assert "value" not in result["metrics"][0]


def test_query_mimir_step_defaults_to_bounded_point_count():
    """An omitted step must not let a long window explode into a huge payload."""
    client = DummyMimirClient()
    client._make_get_request.return_value = {"data": {"result": []}}

    # 24h window: at the 15s floor this would be 5760 points per series.
    client.query_mimir("cdb_cpu_use_rate", start="2026-08-31T00:00:00Z", end="2026-09-01T00:00:00Z")

    _url, kwargs = client._make_get_request.call_args
    assert kwargs["params"]["step"] == "432s"


def test_query_mimir_clamps_a_step_that_would_overflow_the_context():
    """A caller-supplied step is raised to the floor that bounds the point count."""
    client = DummyMimirClient()
    client._make_get_request.return_value = {"data": {"result": []}}

    client.query_mimir(
        "cdb_cpu_use_rate",
        start="2026-08-31T00:00:00Z",
        end="2026-09-01T00:00:00Z",
        step="1s",
    )

    _url, kwargs = client._make_get_request.call_args
    assert kwargs["params"]["step"] == "432s"


def test_query_mimir_short_window_keeps_a_fine_step():
    """A 30-minute window stays at the 15s floor — no needless downsampling."""
    client = DummyMimirClient()
    client._make_get_request.return_value = {"data": {"result": []}}

    client.query_mimir("cdb_cpu_use_rate", start="2026-08-31T05:00:00Z", end="2026-08-31T05:30:00Z")

    _url, kwargs = client._make_get_request.call_args
    assert kwargs["params"]["step"] == "15s"


def test_query_mimir_caps_series_and_reports_the_truncation():
    """An unfiltered query can return thousands of series — cap and say so.

    Silently returning them would be evicted by the context budget, destroying
    the curve data without any signal the agent could react to.
    """
    client = DummyMimirClient()
    client._make_get_request.return_value = {
        "data": {
            "result": [
                {"metric": {"instance": f"host-{i}"}, "values": [[1756616400, "1"]]}
                for i in range(4894)
            ]
        }
    }

    result = client.query_mimir("up", start="2026-08-31T05:00:00Z", end="2026-08-31T05:30:00Z")

    assert result["success"] is True
    assert len(result["metrics"]) == 50
    assert result["total_series"] == 4894
    assert result["truncated_series"] == 4844
    # A bare count is not actionable — the agent must be told what to do next.
    assert "50" in result["truncation_hint"]
    assert "4894" in result["truncation_hint"]
    assert "label" in result["truncation_hint"]


def test_query_mimir_does_not_flag_truncation_when_nothing_was_dropped():
    client = DummyMimirClient()
    client._make_get_request.return_value = {
        "data": {"result": [{"metric": {"instance": "host-1"}, "value": [1756616400, "1"]}]}
    }

    result = client.query_mimir("up")

    assert "truncated_series" not in result
    assert "truncation_hint" not in result


@pytest.mark.parametrize(
    "window",
    [{"start": "2026-08-31T05:00:00Z"}, {"end": "2026-08-31T05:30:00Z"}],
    ids=["start-only", "end-only"],
)
def test_query_mimir_rejects_a_half_open_window(window: dict[str, str]):
    """``start`` and ``end`` are independently optional in the tool schema.

    Falling back to an instant query would answer with the current sample while
    the caller believes it asked about the incident window — plausible evidence
    that is silently wrong about time.
    """
    client = DummyMimirClient()

    result = client.query_mimir("cdb_cpu_use_rate", **window)

    assert result["success"] is False
    assert "start" in result["error"] and "end" in result["error"]
    assert result["metrics"] == []
    client._make_get_request.assert_not_called()


@pytest.mark.parametrize("step", ["nan", "inf"], ids=["nan", "inf"])
def test_query_mimir_falls_back_on_a_non_finite_step(step: str):
    """``math.ceil`` raises on nan/inf, and the step is resolved before the
    request's try block — so a nonsense step would escape the error envelope.

    Unparseable steps already fall back to the floor; non-finite ones must too.
    """
    client = DummyMimirClient()
    client._make_get_request.return_value = {"data": {"result": []}}

    result = client.query_mimir(
        "cdb_cpu_use_rate",
        start="2026-08-31T05:00:00Z",
        end="2026-08-31T05:30:00Z",
        step=step,
    )

    assert result["success"] is True
    _url, kwargs = client._make_get_request.call_args
    assert kwargs["params"]["step"] == "15s"


@pytest.mark.parametrize(
    ("start", "end"),
    [("0", "inf"), ("inf", "0"), ("0", "nan")],
    ids=["inf-end", "inf-start", "nan-end"],
)
def test_query_mimir_survives_a_non_finite_timestamp(start: str, end: str):
    """Unix-seconds endpoints go through ``float``, which accepts inf/nan, and
    the step is resolved before the request's try block.

    Timestamp validity is Mimir's call, not ours — pass the window through and
    let the response carry the error, rather than raising past the envelope.
    """
    client = DummyMimirClient()
    client._make_get_request.return_value = {"data": {"result": []}}

    result = client.query_mimir("cdb_cpu_use_rate", start=start, end=end)

    assert result["success"] is True
    _url, kwargs = client._make_get_request.call_args
    assert kwargs["params"]["step"] == "15s"


@pytest.mark.parametrize(
    ("step", "expected"),
    [("1w", "604800s"), ("1h30m", "5400s"), ("1y", "31536000s"), ("2d", "172800s")],
)
def test_query_mimir_honours_full_prometheus_durations(step: str, expected: str):
    """Prometheus durations use ms/s/m/h/d/w/y and compose (``1h30m``).

    Treating a valid coarse step as garbage silently substitutes the floor,
    returning ~200 points where the caller explicitly asked for far fewer.
    """
    client = DummyMimirClient()
    client._make_get_request.return_value = {"data": {"result": []}}

    # A 7-day window: the computed floor is 3024s, so a coarser step must win.
    client.query_mimir(
        "cdb_cpu_use_rate", start="2026-08-24T00:00:00Z", end="2026-08-31T00:00:00Z", step=step
    )

    _url, kwargs = client._make_get_request.call_args
    assert kwargs["params"]["step"] == expected


def test_query_mimir_drops_a_sample_at_the_window_end():
    """IncidentWindow is ``[since, until)`` but Prometheus evaluates ``end``
    inclusively, so an aligned scrape lands one point past the window.
    """
    client = DummyMimirClient()
    client._make_get_request.return_value = {
        "data": {
            "result": [
                {
                    "metric": {"__name__": "cdb_cpu_use_rate"},
                    "values": [
                        [1788152400, "10"],  # 05:00:00 — inside
                        [1788154185, "20"],  # 05:29:45 — inside
                        [1788154200, "30"],  # 05:30:00 — the exclusive end
                    ],
                }
            ]
        }
    }

    result = client.query_mimir(
        "cdb_cpu_use_rate", start="2026-08-31T05:00:00Z", end="2026-08-31T05:30:00Z"
    )

    assert result["metrics"][0]["values"] == [[1788152400, "10"], [1788154185, "20"]]


@pytest.mark.parametrize(
    "step", ["1h30x", "5x", "w"], ids=["trailing-junk", "bad-unit", "no-count"]
)
def test_query_mimir_falls_back_on_an_unparseable_step(step: str):
    """Genuine garbage still degrades to the floor, as an omitted step does."""
    client = DummyMimirClient()
    client._make_get_request.return_value = {"data": {"result": []}}

    client.query_mimir(
        "cdb_cpu_use_rate", start="2026-08-24T00:00:00Z", end="2026-08-31T00:00:00Z", step=step
    )

    _url, kwargs = client._make_get_request.call_args
    assert kwargs["params"]["step"] == "3024s"
