"""Runtime control over how much of the log stream reaches an operator.

The shell installs its handler at ERROR and the root logger is never lowered
from its WARNING default, so every ``logger.debug`` and ``logger.info`` in the
codebase is invisible by default. That is the right default for a product
surface, but it leaves no way to watch a request travel through the system
without editing code. ``OPENSRE_LOG_LEVEL`` and ``/loglevel`` open that door.
"""

from __future__ import annotations

import logging
import os

from config.constants.logging import OPENSRE_LOG_LEVEL_ENV

DEFAULT_LOG_LEVEL = logging.ERROR

#: Marks a handler that holds its own level. ``set_log_level`` leaves it alone,
#: so a file sink stays at DEBUG while the terminal is raised to ERROR. An
#: attribute rather than a type check: the file handler imports this module.
FIXED_LEVEL_ATTR = "opensre_fixed_level"


def parse_log_level(value: str) -> int | None:
    """Return the level ``value`` names, or ``None`` when it names none.

    Accepts a level name in any case and a bare numeric level.
    """
    text = (value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    resolved = logging.getLevelName(text.upper())
    return resolved if isinstance(resolved, int) else None


def configured_log_level() -> int | None:
    """Return the level the operator asked for, or ``None`` when they asked for none.

    Callers that would override a wider logging configuration — the root logger
    and any handler an embedding host installed — must act only on an explicit
    request, which is what ``None`` here distinguishes.
    """
    return parse_log_level(os.getenv(OPENSRE_LOG_LEVEL_ENV, ""))


def resolve_log_level() -> int:
    """Return the configured level, falling back to ``DEFAULT_LOG_LEVEL``.

    An unusable value falls back rather than raising: a typo in an env var must
    not stop the shell from starting, nor silence its own error reporting.
    """
    level = configured_log_level()
    # NOTSET is a real level and the only falsy one, so truthiness loses it.
    return DEFAULT_LOG_LEVEL if level is None else level


def set_log_level(level: int) -> None:
    """Set the terminal's level on the root logger and the handlers it steers.

    Both are needed: a handler admitting DEBUG sees nothing while the root
    logger still filters at WARNING.

    A handler marked with :data:`FIXED_LEVEL_ATTR` keeps its own floor, and the
    root logger drops to the lowest floor still wanted — otherwise raising the
    terminal to ERROR would silence a file sink deliberately left at DEBUG,
    since the root logger filters before any handler runs.
    """
    root = logging.getLogger()
    floors = [level]
    for handler in root.handlers:
        if getattr(handler, FIXED_LEVEL_ATTR, False):
            floors.append(handler.level)
        else:
            handler.setLevel(level)
    root.setLevel(min(floors))


__all__ = [
    "DEFAULT_LOG_LEVEL",
    "FIXED_LEVEL_ATTR",
    "configured_log_level",
    "parse_log_level",
    "resolve_log_level",
    "set_log_level",
]
