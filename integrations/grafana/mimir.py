"""Mimir metrics query mixin for Grafana Cloud client."""

from __future__ import annotations

import logging
import math
import re
from typing import TYPE_CHECKING, Any

from core.domain.types.incident_anchors import parse_iso8601
from infrastructure.evidence.series_bounds import (
    MAX_POINTS_PER_SERIES,
    MAX_SERIES,
    bound_series,
    truncation_hint,
)

if TYPE_CHECKING:
    from integrations.grafana.base import GrafanaClientBase

# A bare Prometheus metric name is the only shape a trailing ``{label="v"}``
# selector can legally be appended to.
logger = logging.getLogger(__name__)

_BARE_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")

_MIN_STEP_SECONDS = 15

_DURATION_UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31536000,
}
# Prometheus durations compose ("1h30m"). ``ms`` precedes ``m`` because
# alternation matches leftmost-first.
_DURATION_PART = re.compile(r"(\d+)(ms|s|m|h|d|w|y)")


def _parse_seconds(value: str) -> float | None:
    """Parse a bare seconds count, rejecting the ``nan``/``inf`` ``float`` accepts.

    ``math.ceil`` cannot convert a non-finite value, so letting one through
    would raise past the caller's error envelope.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) else None


def _parse_timestamp(value: str) -> float | None:
    """Accept the RFC 3339 and Unix-seconds forms the Prometheus API allows."""
    parsed = parse_iso8601(value)
    if parsed is not None:
        return parsed.timestamp()
    return _parse_seconds(value)


def _parse_step_seconds(step: str) -> float | None:
    """Parse a Prometheus step (``"30s"``, ``"1h30m"``, or bare seconds).

    Returns ``None`` for anything unusable so the caller falls back to the
    computed floor instead of raising.
    """
    text = step.strip()
    if not text:
        return None
    parts = _DURATION_PART.findall(text)
    # Every character must belong to a unit, or "1h30x" would parse as "1h30".
    if parts and "".join(count + unit for count, unit in parts) == text:
        return sum(float(count) * _DURATION_UNIT_SECONDS[unit] for count, unit in parts)
    return _parse_seconds(text)


def _resolve_step(start: str, end: str, step: str | None) -> str:
    """Return the step to send, in seconds, raised to the point-count floor.

    A caller-supplied step is honoured unless it would produce more than
    ``MAX_POINTS_PER_SERIES`` points. An unparseable window falls back to the
    ``_MIN_STEP_SECONDS`` floor rather than failing the query.
    """
    requested = _parse_step_seconds(step) if step else None
    started = _parse_timestamp(start)
    ended = _parse_timestamp(end)

    floor = float(_MIN_STEP_SECONDS)
    if started is not None and ended is not None and ended > started:
        floor = max(floor, (ended - started) / MAX_POINTS_PER_SERIES)

    return f"{math.ceil(max(requested or 0.0, floor))}s"


def _before(values: Any, end_epoch: float | None) -> list[Any]:
    """Drop samples at or after ``end``.

    ``IncidentWindow`` is ``[since, until)`` while Prometheus evaluates
    ``end`` inclusively, so an aligned scrape would otherwise land one point
    outside the window the caller asked about.
    """
    if not isinstance(values, list):
        return []
    if end_epoch is None:
        return values
    kept = []
    for point in values:
        if isinstance(point, (list, tuple)) and point:
            try:
                if float(point[0]) >= end_epoch:
                    continue
            except (TypeError, ValueError):
                pass
        kept.append(point)
    return kept


def _normalize_series(
    series: dict[str, Any], *, ranged: bool, end_epoch: float | None = None
) -> dict[str, Any]:
    """Keep the curve for range results and the single sample for instant ones."""
    normalized: dict[str, Any] = {"metric": series.get("metric", {})}
    if ranged:
        normalized["values"] = _before(series.get("values", []), end_epoch)
    else:
        normalized["value"] = series.get("value", [])
    return normalized


class MimirMixin:
    """Mixin providing Mimir metrics query capabilities."""

    def query_mimir(  # type: ignore[misc]
        self: GrafanaClientBase,
        metric_name: str,
        service_name: str | None = None,
        start: str | None = None,
        end: str | None = None,
        step: str | None = None,
    ) -> dict[str, Any]:
        """Query Grafana Cloud Mimir for metrics.

        With both ``start`` and ``end`` this issues a range query and each
        series carries ``values`` (a curve); otherwise it stays an instant
        query and each series carries a single ``value``. Series are capped at
        ``_MAX_SERIES`` with the dropped count reported as ``truncated_series``.

        Args:
            metric_name: Prometheus metric name or expression.
            service_name: Optional service filter; only valid on a bare metric name.
            start: Window start, RFC 3339 or Unix seconds.
            end: Window end, RFC 3339 or Unix seconds.
            step: Resolution; raised to the floor bounding points per series.

        Returns:
            Dictionary with metric series and values
        """
        if not self.is_configured:
            return {
                "success": False,
                "error": f"Grafana client not configured for account '{self.account_id}'",
                "metrics": [],
            }

        query = metric_name
        if service_name:
            if not _BARE_METRIC_NAME.match(metric_name.strip()):
                return {
                    "success": False,
                    "error": (
                        "service_name can only filter a bare metric name; "
                        f"put the label inside the expression instead of {metric_name!r}"
                    ),
                    "metrics": [],
                }
            query = f'{metric_name}{{service_name="{service_name}"}}'

        if bool(start) != bool(end):
            return {
                "success": False,
                "error": (
                    "a time window needs both start and end; one alone would "
                    "silently return the current sample instead of the window"
                ),
                "metrics": [],
            }

        ranged = bool(start and end)
        if start and end:
            url = self._build_datasource_url(
                self.mimir_datasource_uid,
                "/api/v1/query_range",
            )
            params = {
                "query": query,
                "start": start,
                "end": end,
                "step": _resolve_step(start, end, step),
            }
        else:
            url = self._build_datasource_url(
                self.mimir_datasource_uid,
                "/api/v1/query",
            )
            params = {"query": query}

        # The expression that left the process is the first thing anyone wants
        # when a metric query comes back empty, and it appears in no event,
        # span or transcript.
        logger.debug(
            "mimir query endpoint=%s uid=%s promql=%s window=%s→%s step=%s",
            "query_range" if ranged else "query",
            self.mimir_datasource_uid,
            query,
            params.get("start", "-"),
            params.get("end", "-"),
            params.get("step", "-"),
        )
        try:
            data = self._make_get_request(url, params=params)
            result = data.get("data", {}).get("result", [])

            end_epoch = _parse_timestamp(end) if ranged and end else None
            metrics = bound_series(
                [
                    _normalize_series(series, ranged=ranged, end_epoch=end_epoch)
                    for series in result[:MAX_SERIES]
                ]
            )

            payload: dict[str, Any] = {
                "success": True,
                "metrics": metrics,
                "total_series": len(result),
                "query": query,
                "account_id": self.account_id,
            }
            if len(result) > MAX_SERIES:
                payload["truncated_series"] = len(result) - MAX_SERIES
                payload["truncation_hint"] = truncation_hint(MAX_SERIES, len(result))
                # Dropping most of the series changes what the diagnosis rests
                # on; the agent is told, and so is whoever reads the logs.
                logger.info(
                    "mimir query capped at %d of %d series for %s",
                    MAX_SERIES,
                    len(result),
                    query,
                )
            return payload
        except Exception as e:
            error_msg = str(e)
            response_text = ""
            if hasattr(e, "response") and e.response is not None:
                response_text = e.response.text[:300]
                error_msg = f"Mimir query failed: {e.response.status_code}"

            # An expired token or a PromQL syntax error otherwise reaches only
            # the model, as an `available: false` result nobody else ever sees.
            logger.warning("mimir query failed promql=%s error=%s", query, error_msg)
            return {
                "success": False,
                "error": error_msg,
                "response": response_text,
                "metrics": [],
            }
