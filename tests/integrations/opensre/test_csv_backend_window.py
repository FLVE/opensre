"""The CSV replay backend must honour the incident window it is given."""

from __future__ import annotations

from pathlib import Path

from integrations.opensre.csv_grafana_backend import OpenSRECsvGrafanaBackend

_ROWS = """timestamp,value
2026-08-31T04:00:00Z,10
2026-08-31T05:10:00Z,20
2026-08-31T05:20:00Z,30
2026-08-31T09:00:00Z,40
"""


def _backend(tmp_path: Path) -> OpenSRECsvGrafanaBackend:
    metric_dir = tmp_path / "metric"
    metric_dir.mkdir()
    (metric_dir / "cpu.csv").write_text(_ROWS)
    return OpenSRECsvGrafanaBackend(telemetry_dir=tmp_path)


def _values(payload: dict) -> list:
    result = payload.get("data", {}).get("result", [])
    return result[0].get("values", []) if result else []


def test_query_timeseries_filters_rows_to_the_window(tmp_path: Path) -> None:
    """The metrics tool now passes the incident window down. Ignoring it returns
    whole-day evidence under a citation that names 30 minutes.
    """
    payload = _backend(tmp_path).query_timeseries(
        query="cpu", start="2026-08-31T05:00:00Z", end="2026-08-31T05:30:00Z"
    )

    assert [float(value) for _ts, value in _values(payload)] == [20.0, 30.0]


def test_query_timeseries_without_a_window_returns_everything(tmp_path: Path) -> None:
    """Windowless replay keeps its existing behaviour."""
    payload = _backend(tmp_path).query_timeseries(query="cpu")

    assert len(_values(payload)) == 4


def test_query_timeseries_ignores_an_unparseable_bound(tmp_path: Path) -> None:
    """A bound we cannot read must not silently empty the result set."""
    payload = _backend(tmp_path).query_timeseries(query="cpu", start="not-a-time", end="also-not")

    assert len(_values(payload)) == 4


def test_query_timeseries_excludes_the_window_end(tmp_path: Path) -> None:
    """IncidentWindow is ``[since, until)``: a sample at ``until`` is outside."""
    payload = _backend(tmp_path).query_timeseries(
        query="cpu", start="2026-08-31T05:10:00Z", end="2026-08-31T05:20:00Z"
    )

    assert [float(value) for _ts, value in _values(payload)] == [20.0]


def _day_of_samples(tmp_path: Path, *, every_seconds: int = 15) -> OpenSRECsvGrafanaBackend:
    metric_dir = tmp_path / "metric"
    metric_dir.mkdir()
    rows = ["timestamp,value"]
    for i in range(86_400 // every_seconds):
        second = i * every_seconds
        stamp = f"2026-08-31T{second // 3600:02d}:{second % 3600 // 60:02d}:{second % 60:02d}Z"
        rows.append(f"{stamp},{i}")
    (metric_dir / "cpu.csv").write_text("\n".join(rows))
    return OpenSRECsvGrafanaBackend(telemetry_dir=tmp_path)


def test_a_window_past_the_row_cap_still_returns_evidence(tmp_path: Path) -> None:
    """The row cap keeps a file's first N rows. Applying it before the window
    filter makes any incident in the later part of the day read as "no data" —
    worse than no data, because it looks like the metric was flat.
    """
    backend = _day_of_samples(tmp_path)

    payload = backend.query_timeseries(
        query="cpu", start="2026-08-31T22:00:00Z", end="2026-08-31T22:30:00Z"
    )

    assert len(_values(payload)) == 120  # 30 min at 15s, end exclusive
