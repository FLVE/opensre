"""Bounds on the metric payload an investigation transcript can carry.

Both axes matter: an unfiltered query returns thousands of series, and a long
window at a fine resolution returns thousands of points each. Past the context
budget the trimmer evicts them silently, so the caller has to cap first and say
that it did.
"""

from __future__ import annotations

from typing import Any

MAX_SERIES = 50
MAX_POINTS_PER_SERIES = 200

_TRUNCATION_HINT = (
    "Showing {kept} of {total} series. Add label selectors to the query to narrow the result set."
)


def truncation_hint(kept: int, total: int) -> str:
    """Tell the agent what was dropped and how to avoid dropping it."""
    return _TRUNCATION_HINT.format(kept=kept, total=total)


def _downsample(values: list[Any], max_points: int) -> list[Any]:
    """Keep at most ``max_points``, evenly spaced, preserving the last sample."""
    if len(values) <= max_points:
        return values
    stride = len(values) / max_points
    thinned = [values[int(i * stride)] for i in range(max_points)]
    if thinned[-1] is not values[-1]:
        thinned[-1] = values[-1]
    return thinned


def bound_series(
    series: list[dict[str, Any]],
    *,
    max_series: int = MAX_SERIES,
    max_points: int = MAX_POINTS_PER_SERIES,
) -> list[dict[str, Any]]:
    """Return at most ``max_series`` entries, each thinned to ``max_points``.

    Series are dropped from the tail rather than merged: the caller reports the
    real total separately, so a reader can see that a narrower query is needed.
    """
    bounded: list[dict[str, Any]] = []
    for item in series[:max_series]:
        values = item.get("values")
        if isinstance(values, list) and len(values) > max_points:
            item = {**item, "values": _downsample(values, max_points)}
        bounded.append(item)
    return bounded


__all__ = ["MAX_POINTS_PER_SERIES", "MAX_SERIES", "bound_series", "truncation_hint"]
