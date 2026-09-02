"""Root log level control, without which no debug logging is ever visible."""

from __future__ import annotations

import logging

import pytest

from infrastructure.logging import configured_log_level, resolve_log_level, set_log_level


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("DEBUG", logging.DEBUG),
        ("debug", logging.DEBUG),
        (" info ", logging.INFO),
        ("WARNING", logging.WARNING),
        ("10", logging.DEBUG),
    ],
    ids=["upper", "lower", "padded", "warning", "numeric"],
)
def test_resolve_log_level_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch, configured: str, expected: int
) -> None:
    monkeypatch.setenv("OPENSRE_LOG_LEVEL", configured)

    assert resolve_log_level() == expected


@pytest.mark.parametrize("configured", ["", "  ", "LOUD", "nonsense"], ids=list("abcd"))
def test_resolve_log_level_falls_back_when_unusable(
    monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    """A typo must not silence the shell's own ERROR output."""
    monkeypatch.setenv("OPENSRE_LOG_LEVEL", configured)

    assert resolve_log_level() == logging.ERROR


def test_resolve_log_level_defaults_to_error_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell is a product surface; a WARNING duplicates what it already says."""
    monkeypatch.delenv("OPENSRE_LOG_LEVEL", raising=False)

    assert resolve_log_level() == logging.ERROR


def test_resolve_log_level_keeps_notset(monkeypatch: pytest.MonkeyPatch) -> None:
    """``NOTSET`` is a real level and the only falsy one; truthiness loses it."""
    monkeypatch.setenv("OPENSRE_LOG_LEVEL", "0")

    assert resolve_log_level() == logging.NOTSET


def test_configured_log_level_is_none_when_the_operator_asked_for_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shell must leave an embedding host's logging alone unless asked.

    ``install_shell_log_handler`` deliberately skips a root logger that already
    has handlers; lowering the root logger anyway would undo that.
    """
    monkeypatch.delenv("OPENSRE_LOG_LEVEL", raising=False)

    assert configured_log_level() is None


def test_configured_log_level_reports_an_explicit_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSRE_LOG_LEVEL", "debug")

    assert configured_log_level() == logging.DEBUG


def test_set_log_level_lowers_the_root_logger_and_its_handlers() -> None:
    """The root logger defaults to WARNING, so a handler alone cannot see DEBUG."""
    root = logging.getLogger()
    handler = logging.NullHandler()
    handler.setLevel(logging.ERROR)
    root.addHandler(handler)
    original_level = root.level
    try:
        set_log_level(logging.DEBUG)

        assert root.level == logging.DEBUG
        assert handler.level == logging.DEBUG
    finally:
        root.removeHandler(handler)
        root.setLevel(original_level)
