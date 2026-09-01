"""Seed calls must be schema-valid without fabricating arguments."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from core.domain.types.retrieval import RetrievalControls
from core.tool.contracts import RegisteredTool
from tools.investigation.stages.gather_evidence.tools import build_seed_calls


def _grafana_tool(
    name: str,
    *,
    required: list[str] | None = None,
    extract_params: Callable[[dict[str, dict]], dict[str, Any]] | None = None,
) -> RegisteredTool:
    def _run(**_kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {key: {"type": "string"} for key in required or []},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required

    # RegisteredTool rejects an explicit None extract_params; omit it instead so
    # the default no-params behaviour applies.
    optional: dict[str, Any] = {"extract_params": extract_params} if extract_params else {}
    return RegisteredTool(
        name=name,
        description=f"{name} tool",
        input_schema=schema,
        source="grafana",  # type: ignore[arg-type]
        run=cast(Callable[..., Any], _run),
        use_cases=[],
        retrieval_controls=RetrievalControls(),
        **optional,
    )


def _grafana_state() -> dict[str, Any]:
    return {
        "alert_source": "grafana",
        "resolved_integrations": {"grafana": {"endpoint": "http://grafana", "api_key": "k"}},
    }


def test_seed_is_skipped_when_a_required_arg_cannot_be_supplied() -> None:
    """A deterministic seed has no model to ask, so an unsatisfiable required
    arg means the call must be dropped — not sent as an invalid payload, and
    not papered over with a fabricated default that queries the wrong thing.
    """
    tool = _grafana_tool("needs_metric_name", required=["metric_name"])

    assert build_seed_calls(_grafana_state(), [tool], object()) == []


def test_seed_is_kept_when_extract_params_supplies_the_required_arg() -> None:
    def _params(_sources: dict[str, dict]) -> dict[str, Any]:
        return {"log_group": "/aws/lambda/fn"}

    tool = _grafana_tool("needs_log_group", required=["log_group"], extract_params=_params)

    calls = build_seed_calls(_grafana_state(), [tool], object())

    assert [call.input for call in calls] == [{"log_group": "/aws/lambda/fn"}]


def test_seed_is_kept_when_the_tool_needs_no_arguments() -> None:
    """Discovery tools are the seeds that make sense without arguments."""
    tool = _grafana_tool("lists_alert_rules")

    calls = build_seed_calls(_grafana_state(), [tool], object())

    assert [call.name for call in calls] == ["lists_alert_rules"]
