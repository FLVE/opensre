"""Logging at the points a request's shape is actually decided.

Runtime events and spans already carry counts and durations. What none of them
carry is content: the arguments a tool was called with, the PromQL that left
the process, the SQL that reached the database, which datasource answered.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from integrations.grafana.mimir import MimirMixin


class _Probe(MimirMixin):
    def __init__(self) -> None:
        self.is_configured = True
        self.account_id = "acc"
        self.mimir_datasource_uid = "uid-1"
        self._build_datasource_url = MagicMock(
            side_effect=lambda uid, path: f"https://g/{uid}{path}"
        )
        self._make_get_request = MagicMock(return_value={"data": {"result": []}})


def _messages(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if needle in record.getMessage()]


def test_mimir_query_logs_the_promql_and_window(caplog: pytest.LogCaptureFixture) -> None:
    """The expression that left the process is the single most useful fact when
    a metric query comes back empty, and nothing records it today.
    """
    caplog.set_level(logging.DEBUG, logger="integrations.grafana.mimir")

    _Probe().query_mimir(
        'cdb_cpu_use_rate{instanceid="x"}',
        start="2026-08-31T05:00:00Z",
        end="2026-08-31T05:30:00Z",
    )

    logged = _messages(caplog, "mimir query")
    assert logged
    assert "cdb_cpu_use_rate" in logged[0]
    assert "query_range" in logged[0]


def test_mimir_failure_is_logged_not_only_returned(caplog: pytest.LogCaptureFixture) -> None:
    """A bad token or a PromQL syntax error becomes an ``available: false`` tool
    result the model reads and the operator never sees.
    """
    caplog.set_level(logging.DEBUG, logger="integrations.grafana.mimir")
    probe = _Probe()
    probe._make_get_request.side_effect = RuntimeError("boom")

    result = probe.query_mimir("up")

    assert result["success"] is False
    assert _messages(caplog, "mimir query failed")


def test_series_truncation_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Dropping 4,844 of 4,894 series changes what the diagnosis rests on."""
    caplog.set_level(logging.DEBUG, logger="integrations.grafana.mimir")
    probe = _Probe()
    probe._make_get_request.return_value = {
        "data": {"result": [{"metric": {"i": str(i)}, "value": [1, "1"]} for i in range(80)]}
    }

    probe.query_mimir("up")

    assert _messages(caplog, "capped")


class _Config:
    is_configured = True
    host = "db-1.internal"
    port = 3306
    database = "orders"


def test_a_relational_query_logs_its_target_and_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The relational integrations have no logging at all: not the host, not
    which diagnostic ran, not how long it took. One decorator covers MySQL,
    Postgres and MariaDB together.
    """
    from integrations._relational import read_only_query

    caplog.set_level(logging.DEBUG, logger=__name__)
    probe_logger = logging.getLogger(__name__)

    @read_only_query(integration="mysql", logger=probe_logger, connect=lambda _config: MagicMock())
    def get_slow_queries(_cursor: Any, _config: Any) -> dict[str, Any]:
        return {"rows": 2}

    assert get_slow_queries(_Config()) == {"rows": 2}

    started = _messages(caplog, "mysql query start")
    assert started
    assert "get_slow_queries" in started[0]
    assert "db-1.internal:3306/orders" in started[0]
    assert _messages(caplog, "mysql query done")


def test_a_failing_relational_query_says_which_one_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Today a failure yields a WARNING with no traceback and a
    ``tool_unavailable`` the model reads; the operator gets no timing or target.
    """
    from integrations._relational import read_only_query

    caplog.set_level(logging.DEBUG, logger=__name__)
    probe_logger = logging.getLogger(__name__)

    def _explode(_config: Any) -> Any:
        raise RuntimeError("connection refused")

    @read_only_query(integration="mysql", logger=probe_logger, connect=_explode)
    def get_current_processes(_cursor: Any, _config: Any) -> dict[str, Any]:
        return {}

    result = get_current_processes(_Config())

    assert result["available"] is False
    assert _messages(caplog, "mysql query failed")


def test_datasource_choice_records_where_the_uid_came_from(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery picks ``isDefault``; a configured UID overrides it. Which one
    won explains an empty result set faster than anything else.
    """
    from integrations.grafana import client as grafana_client

    caplog.set_level(logging.DEBUG, logger="integrations.grafana.client")
    grafana_client._grafana_client_cache.clear()
    monkeypatch.setenv("GRAFANA_CONFIG_SKIP_ENV_FILE", "1")
    monkeypatch.setenv("GRAFANA_MIMIR_DATASOURCE_UID", "configured-mimir")
    monkeypatch.setattr(
        grafana_client.GrafanaClient,
        "discover_datasource_uids",
        lambda _self: {"mimir_uid": "discovered-mimir"},
    )

    grafana_client.get_grafana_client_from_credentials(
        endpoint="http://grafana.example.com", api_key="token", account_id="log_probe"
    )
    grafana_client._grafana_client_cache.clear()

    logged = _messages(caplog, "datasource mimir")
    assert logged
    assert "configured" in logged[0]


def test_tool_call_start_names_the_arguments_without_their_free_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`tool_call start` records the tool name but not which arguments it got,
    so a call made with the wrong fields is invisible at the dispatch layer.

    The values stay out: key-name redaction cannot see a credential embedded in
    an ordinary field such as a shell command or a connection string, and this
    log reaches the terminal and a hosted gateway's server log.
    """
    from core.tool.execution import _log_tool_call_start

    caplog.set_level(logging.DEBUG, logger="core.tool.execution")

    _log_tool_call_start(
        name="shell_run",
        call_id="c1",
        source="system",
        tool_input={"command": "curl -H 'Authorization: Bearer sk-live-1'", "timeout": 30},
    )

    logged = _messages(caplog, "tool_call start")
    assert logged
    assert "command" in logged[0]
    assert "sk-live-1" not in logged[0]
    # A number cannot carry a credential, and a wrong one explains a failure.
    assert "timeout=30" in logged[0]


def test_tool_call_start_stays_on_one_terminal_row(caplog: pytest.LogCaptureFixture) -> None:
    """``ShellLogHandler`` prints one row per record while a Rich spinner
    animates; an embedded newline invalidates Live's cursor accounting and
    staircases everything printed afterwards.
    """
    from core.tool.execution import _log_tool_call_start

    caplog.set_level(logging.DEBUG, logger="core.tool.execution")

    _log_tool_call_start(
        name="query_grafana_metrics",
        call_id="c1",
        source="grafana",
        tool_input={"metric_name": "up", "start": "2026-08-31T05:00:00Z", "step": "15s"},
    )

    logged = _messages(caplog, "tool_call start")
    assert logged
    assert "\n" not in logged[0]
