"""Metrics split across independent Prometheus datasources.

A Grafana instance can front several Prometheus servers whose metric coverage
does not overlap — a container one and a database one, say. A client holding a
single UID can only ever see one of them, which is enough to make a whole class
of incident invisible: "is the database slow because a co-tenant container took
the CPU" spans both.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from integrations.grafana.mimir import MimirMixin

_DATABASE_UID = "wloGDWa4k"
_CONTAINER_UID = "f304245b-27a7-426c-a9cc-2fc795dd8e02"


class _Probe(MimirMixin):
    def __init__(self, *uids: str) -> None:
        self.is_configured = True
        self.account_id = "acc"
        self.mimir_datasource_uid = uids[0] if uids else ""
        self.mimir_datasource_uids = tuple(uids)
        self._build_datasource_url = MagicMock(
            side_effect=lambda uid, path: f"https://g/{uid}{path}"
        )
        self._make_get_request = MagicMock()


def _series(name: str) -> dict[str, Any]:
    return {"metric": {"__name__": name}, "value": [1, "1"]}


def _uids_queried(probe: _Probe) -> list[str]:
    return [call.args[0] for call in probe._build_datasource_url.call_args_list]


def test_the_first_datasource_that_answers_ends_the_search() -> None:
    """The common case must not pay for the fallback."""
    probe = _Probe(_DATABASE_UID, _CONTAINER_UID)
    probe._make_get_request.return_value = {"data": {"result": [_series("cdb_cpu_use_rate")]}}

    result = probe.query_mimir("cdb_cpu_use_rate")

    assert result["success"] is True
    assert _uids_queried(probe) == [_DATABASE_UID]


def test_an_empty_first_datasource_falls_through_to_the_next() -> None:
    """``kube_pod_info`` does not exist on the database Prometheus at all."""
    probe = _Probe(_DATABASE_UID, _CONTAINER_UID)
    probe._make_get_request.side_effect = [
        {"data": {"result": []}},
        {"data": {"result": [_series("kube_pod_info")]}},
    ]

    result = probe.query_mimir("kube_pod_info")

    assert result["success"] is True
    assert result["total_series"] == 1
    assert _uids_queried(probe) == [_DATABASE_UID, _CONTAINER_UID]


def test_a_failing_datasource_does_not_block_the_next_one() -> None:
    """Stopping at the first error would let one dead datasource hide every
    metric on the ones behind it.
    """
    probe = _Probe(_DATABASE_UID, _CONTAINER_UID)
    probe._make_get_request.side_effect = [
        RuntimeError("503 Service Unavailable"),
        {"data": {"result": [_series("kube_pod_info")]}},
    ]

    result = probe.query_mimir("kube_pod_info")

    assert result["success"] is True
    assert _uids_queried(probe) == [_DATABASE_UID, _CONTAINER_UID]


def test_a_metric_absent_everywhere_is_an_empty_success_not_an_error() -> None:
    """An empty window is a finding; turning it into a failure would tell the
    agent the tool is unavailable.
    """
    probe = _Probe(_DATABASE_UID, _CONTAINER_UID)
    probe._make_get_request.return_value = {"data": {"result": []}}

    result = probe.query_mimir("does_not_exist")

    assert result["success"] is True
    assert result["metrics"] == []


def test_when_every_datasource_fails_the_first_error_is_reported() -> None:
    """The last error is usually the least informative one — a bad PromQL fails
    identically everywhere, and the first failure is what the operator hit.
    """
    probe = _Probe(_DATABASE_UID, _CONTAINER_UID)
    probe._make_get_request.side_effect = [
        RuntimeError("400 parse error at char 5"),
        RuntimeError("503 Service Unavailable"),
    ]

    result = probe.query_mimir("cdb_cpu_use_rate{")

    assert result["success"] is False
    assert "parse error" in result["error"]


def test_the_answering_datasource_is_named_in_the_result() -> None:
    """Correlating two metrics that came from different Prometheus servers is
    only defensible if the report can say which one each came from.
    """
    probe = _Probe(_DATABASE_UID, _CONTAINER_UID)
    probe._make_get_request.side_effect = [
        {"data": {"result": []}},
        {"data": {"result": [_series("kube_pod_info")]}},
    ]

    result = probe.query_mimir("kube_pod_info")

    assert result["datasource_uid"] == _CONTAINER_UID


def test_a_client_configured_with_one_uid_behaves_as_before() -> None:
    """Existing installations set only the singular env var."""
    probe = _Probe(_DATABASE_UID)
    probe._make_get_request.return_value = {"data": {"result": []}}

    result = probe.query_mimir("cdb_cpu_use_rate")

    assert result["success"] is True
    assert _uids_queried(probe) == [_DATABASE_UID]


def test_the_tool_output_carries_the_answering_datasource() -> None:
    """Correlating a container metric with a database metric is only auditable
    if the report says which Prometheus each series came from.
    """
    from unittest.mock import patch

    from integrations.grafana.tools.grafana_metrics_tool import (
        _citation_summary,
        query_grafana_metrics,
    )

    client = MagicMock()
    client.is_configured = True
    client.mimir_datasource_uid = _DATABASE_UID
    client.account_id = 1
    client.query_mimir.return_value = {
        "success": True,
        "metrics": [_series("kube_pod_info")],
        "total_series": 1,
        "datasource_uid": _CONTAINER_UID,
    }

    with patch("integrations.grafana.tools._helpers._resolve_grafana_client", return_value=client):
        output = query_grafana_metrics(
            metric_name="kube_pod_info", grafana_endpoint="https://g", grafana_api_key="k"
        )

    assert output["datasource_uid"] == _CONTAINER_UID
    assert _CONTAINER_UID in _citation_summary(output, "kube_pod_info")


def test_a_single_entry_plural_list_still_wins_over_discovery(
    monkeypatch: Any,
) -> None:
    """Listing one UID in the plural variable is a deliberate choice.

    Treating a one-item list as "not configured" falls back to the discovered
    ``isDefault`` datasource — the exact inversion of the documented precedence,
    and the failure mode this whole feature exists to prevent.
    """
    from integrations.grafana import client as grafana_client

    grafana_client._grafana_client_cache.clear()
    monkeypatch.setenv("GRAFANA_CONFIG_SKIP_ENV_FILE", "1")
    monkeypatch.delenv("GRAFANA_MIMIR_DATASOURCE_UID", raising=False)
    monkeypatch.setenv("GRAFANA_MIMIR_DATASOURCE_UIDS", _DATABASE_UID)
    monkeypatch.setattr(
        grafana_client.GrafanaClient,
        "discover_datasource_uids",
        lambda _self: {"mimir_uid": _CONTAINER_UID},
    )

    client = grafana_client.get_grafana_client_from_credentials(
        endpoint="http://grafana.example.com", api_key="token", account_id="single_plural"
    )
    grafana_client._grafana_client_cache.clear()

    assert client.mimir_datasource_uids == (_DATABASE_UID,)


def test_the_new_env_name_is_reachable_through_the_package_facade() -> None:
    """``config/constants/__init__.py`` is the canonical import path; a name
    only in the leaf module breaks every caller that follows the contract.
    """
    from config.constants import GRAFANA_MIMIR_DATASOURCE_UIDS_ENV

    assert GRAFANA_MIMIR_DATASOURCE_UIDS_ENV == "GRAFANA_MIMIR_DATASOURCE_UIDS"
