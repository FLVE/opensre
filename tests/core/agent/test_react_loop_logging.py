"""The ReAct loop is the single chokepoint for every LLM call in the product.

Chat and investigation both pass through it, so one log line here covers both.
Nothing else records which model was asked, with how many tools, or what it
asked to run — the runtime events and spans carry counts, not identities.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from core.agent import Agent
from core.llm.types import AgentLLMResponse, ToolCall

_LOGGER_NAME = "core.agent.react_loop"


class _NoToolLLM:
    model_id = "test-model"

    def tool_schemas(self, _tools: list[Any]) -> list[dict[str, Any]]:
        return []

    def invoke(
        self,
        _messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AgentLLMResponse:
        _ = (system, tools)
        return AgentLLMResponse(content="done", tool_calls=[], raw_content=None)

    @staticmethod
    def build_assistant_message(content: str, tool_calls: list[object]) -> dict[str, object]:
        return {"role": "assistant", "content": content, "tool_calls": tool_calls}

    @staticmethod
    def build_tool_result_message(
        _tool_calls: list[object], _results: list[object]
    ) -> dict[str, object]:
        return {"role": "tool", "content": "[]"}


class _AsksForEchoLLM(_NoToolLLM):
    def __init__(self) -> None:
        self.calls = 0

    def invoke(
        self,
        _messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AgentLLMResponse:
        _ = (system, tools)
        self.calls += 1
        if self.calls == 1:
            return AgentLLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="echo", input={})],
                raw_content=None,
            )
        return AgentLLMResponse(content="done", tool_calls=[], raw_content=None)


class _EchoTool:
    name = "echo"
    description = "echo"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def validate_public_input(self, value: dict[str, Any]) -> str | None:
        _ = value
        return None

    def extract_params(self, resolved: dict[str, Any]) -> dict[str, Any]:
        _ = resolved
        return {}

    def run(self, **_kwargs: Any) -> dict[str, bool]:
        return {"ok": True}


def _messages(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if needle in record.getMessage()]


def test_llm_request_names_the_model_and_what_it_was_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    agent = Agent(
        llm=_NoToolLLM(), system="sys", tools=[], resolved_integrations={}, max_iterations=1
    )

    agent.run([{"role": "user", "content": "hello"}])

    logged = _messages(caplog, "llm request")
    assert logged
    assert "test-model" in logged[0]


def test_llm_response_names_the_tools_it_asked_for(caplog: pytest.LogCaptureFixture) -> None:
    """A turn's shape is decided here; a bare tool_call *count* cannot explain it."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    agent = Agent(
        llm=_AsksForEchoLLM(),
        system="sys",
        tools=[_EchoTool()],
        resolved_integrations={},
        max_iterations=3,
    )

    agent.run([{"role": "user", "content": "hello"}])

    assert any("echo" in message for message in _messages(caplog, "llm response"))


def test_hitting_the_iteration_cap_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Running out of iterations silently is indistinguishable from a real
    answer once the transcript is all you have.
    """
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    agent = Agent(
        llm=_AsksForEchoLLM(),
        system="sys",
        tools=[_EchoTool()],
        resolved_integrations={},
        max_iterations=1,
    )

    agent.run([{"role": "user", "content": "hello"}])

    assert _messages(caplog, "iteration limit")
