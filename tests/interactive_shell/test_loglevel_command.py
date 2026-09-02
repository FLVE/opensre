"""`/loglevel` — the only in-shell way to see the codebase's own logging."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from rich.console import Console

from infrastructure.logging import ShellLogHandler, install_file_log_handler
from surfaces.interactive_shell.command_registry.settings_cmds import COMMANDS, _cmd_loglevel


@pytest.fixture(autouse=True)
def _restore_root_level():
    root = logging.getLogger()
    original = root.level
    original_handlers = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in original_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(original)


def _console() -> Console:
    return Console(force_terminal=False, no_color=True, width=100)


def test_loglevel_is_registered() -> None:
    assert any(command.name == "/loglevel" for command in COMMANDS)


def test_setting_a_level_lowers_the_root_logger() -> None:
    """The handler floor alone is not enough; the root logger filters first."""
    _cmd_loglevel(None, _console(), ["debug"])

    assert logging.getLogger().level == logging.DEBUG


def test_an_unknown_level_is_rejected_without_changing_anything() -> None:
    logging.getLogger().setLevel(logging.ERROR)

    _cmd_loglevel(None, _console(), ["louder"])

    assert logging.getLogger().level == logging.ERROR


def test_bare_loglevel_reports_the_current_level() -> None:
    logging.getLogger().setLevel(logging.INFO)
    console = _console()

    with console.capture() as captured:
        _cmd_loglevel(None, console, [])

    assert "INFO" in captured.get()


def test_bare_loglevel_reports_what_actually_reaches_the_operator() -> None:
    """The root level alone is not what an operator sees.

    On a default start the shell handler sits at ERROR while the root logger
    keeps its WARNING default, so reporting the root level alone tells someone
    hunting for missing logs that WARNING is on when those records are dropped.
    """
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    handler = ShellLogHandler()
    handler.setLevel(logging.ERROR)
    root.addHandler(handler)
    console = _console()
    try:
        with console.capture() as captured:
            _cmd_loglevel(None, console, [])
    finally:
        root.removeHandler(handler)

    assert "ERROR" in captured.get()


def test_bare_loglevel_names_the_file_when_one_is_being_written(tmp_path: Path) -> None:
    """The file holds its own level, so nothing else on screen reveals it."""
    target = tmp_path / "opensre.log"
    # /loglevel reports the sink that is really installed, not the env var.
    install_file_log_handler(target)
    console = _console()

    with console.capture() as captured:
        _cmd_loglevel(None, console, [])

    # The console wraps this fixture's long tmp path; a real one does not.
    assert str(target) in captured.get().replace("\n", "")


def test_a_rejected_level_containing_markup_does_not_break_the_turn() -> None:
    """Rich reads ``[/]`` in an interpolated argument as a closing tag and
    raises ``MarkupError``, which aborts the whole slash-command turn.
    """
    console = _console()

    with console.capture() as captured:
        _cmd_loglevel(None, console, ["[/]"])

    assert "[/]" in captured.get()
