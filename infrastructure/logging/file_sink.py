"""Durable log sink for the detail a terminal scrollback cannot keep.

The shell's handler prints and forgets, and nothing else in the product writes
a log file, so a run that misbehaved once leaves nothing to read afterwards.
``OPENSRE_LOG_FILE`` names a file; unset, nothing is written and no directory
is created.

The file holds its own level, so ``OPENSRE_LOG_FILE_LEVEL=DEBUG`` records
everything on disk while the terminal stays at its quiet default — the
combination that is actually useful while reproducing a problem.

The file is owner-only: at DEBUG it carries tool arguments and outbound URLs,
which the default umask would otherwise publish to every account on the box.
"""

from __future__ import annotations

import logging
import os
import stat
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, cast

from config.constants.logging import (
    DEFAULT_LOG_FILE_BACKUP_COUNT,
    DEFAULT_LOG_FILE_MAX_BYTES,
    OPENSRE_LOG_FILE_ENV,
    OPENSRE_LOG_FILE_LEVEL_ENV,
)
from infrastructure.logging.level import FIXED_LEVEL_ATTR, parse_log_level

logger = logging.getLogger(__name__)

DEFAULT_LOG_FILE_LEVEL = logging.DEBUG

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_FILE_MODE = 0o600
_OPEN_FLAGS = os.O_CREAT | os.O_APPEND | os.O_WRONLY
_FALSE_VALUES = frozenset({"", "0", "false", "off", "no"})


class _OwnerOnlyRotatingSink(RotatingFileHandler):
    """Rotating sink whose files only their owner can read.

    Also the marker for *this* sink: a host may run rotating file handlers of
    its own, and a plain type check would confuse one for the other.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        #: Set once the sink gives up; read by :func:`installed_log_file`.
        self.disabled_by_failure = False
        super().__init__(*args, **kwargs)

    def _open(self) -> TextIOWrapper:
        # Created with the restricted mode rather than chmod'ed afterwards, so
        # no window exists where the file is world-readable. Tightening covers
        # a file an earlier run — or another account — left behind looser, and
        # goes through the descriptor so the path cannot be swapped in between.
        descriptor = os.open(self.baseFilename, _OPEN_FLAGS, _FILE_MODE)
        try:
            self._require_owner_only(descriptor)
        except OSError:
            # Appending to a file this process cannot restrict would publish
            # tool arguments and outbound URLs; refuse rather than weaken it.
            os.close(descriptor)
            raise
        stream = os.fdopen(descriptor, self.mode, encoding=self.encoding)
        return cast(TextIOWrapper, stream)

    @staticmethod
    def _require_owner_only(descriptor: int) -> None:
        """Raise unless the open file ends up readable by its owner alone.

        POSIX modes are meaningless on Windows, which has no ``fchmod``.
        """
        if not hasattr(os, "fchmod"):
            return
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != _FILE_MODE:
            os.fchmod(descriptor, _FILE_MODE)

    def handleError(self, _record: logging.LogRecord) -> None:
        """Disable the sink after one complaint.

        A disk that fills, or a rollover rename that fails, happens long after
        the installer's guard. The stdlib default either drops records without
        a word or prints a traceback per record; neither tells an operator that
        the file they are tailing has stopped growing.
        """
        if self.disabled_by_failure:
            return
        self.disabled_by_failure = True
        # Raised before the complaint is logged: this handler is on the root
        # logger, so an unraised level would route the message back into it.
        self.setLevel(logging.CRITICAL + 1)
        logger.error(
            "log file %s stopped accepting records; logging to the terminal only",
            self.baseFilename,
        )


def log_file_path() -> Path | None:
    """Return the file the operator named, or ``None`` when they named none.

    ``None`` also for a path that cannot be resolved at all: ``expanduser``
    raises for an unknown ``~user``, and that must not abort shell startup.
    """
    configured = os.getenv(OPENSRE_LOG_FILE_ENV, "").strip()
    if not configured:
        return None
    try:
        return Path(configured).expanduser()
    except (RuntimeError, OSError, ValueError) as err:
        logger.error("log file path %r could not be resolved (%s); ignoring it", configured, err)
        return None


def log_file_level() -> int:
    """Return the file's own floor, defaulting to DEBUG.

    Someone who asked for a file wants the detail; inheriting the terminal's
    ERROR default would leave them a nearly empty one.
    """
    level = parse_log_level(os.getenv(OPENSRE_LOG_FILE_LEVEL_ENV, ""))
    return DEFAULT_LOG_FILE_LEVEL if level is None else level


def installed_log_file() -> Path | None:
    """Return the file this process is really writing, or ``None``.

    Read this rather than :func:`log_file_path` before telling an operator that
    a file is being written: the path can be configured and the open still have
    failed.
    """
    for handler in logging.getLogger().handlers:
        # A sink that gave up on a full disk is still attached but will never
        # take another record; naming it would send an operator to a file that
        # stopped growing.
        if isinstance(handler, _OwnerOnlyRotatingSink) and not handler.disabled_by_failure:
            return Path(handler.baseFilename)
    return None


def _running_under_test() -> bool:
    """True inside a pytest or CI run.

    A developer who put ``OPENSRE_LOG_FILE`` in their ``.env`` would otherwise
    have every test session open that file, write to their working tree and
    leave the handler behind for later tests. Same guard, same reason as the
    operations log.
    """
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    return os.getenv("CI", "").strip().lower() not in _FALSE_VALUES


def install_file_log_handler(path: Path | None = None) -> Path | None:
    """Install the rotating file sink; return the path in use, else ``None``.

    ``path`` overrides ``OPENSRE_LOG_FILE`` and is how a test asks for a sink:
    the configured file is ignored under pytest or CI so a test run never
    writes a developer's real one.

    Install this *after* ``install_shell_log_handler``, which skips a root
    logger that already has handlers — reversing the order leaves the terminal
    with no handler at all.

    Never raises: a full disk or a read-only home must not keep the shell from
    starting, so a failure degrades to terminal-only logging. Failures are
    reported at ERROR, the one level the shell's default handler admits.
    """
    if path is None:
        if _running_under_test():
            return None
        path = log_file_path()
    if path is None:
        return None

    root = logging.getLogger()
    installed = installed_log_file()
    if installed is not None:
        return installed

    level = log_file_level()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = _OwnerOnlyRotatingSink(
            path,
            maxBytes=DEFAULT_LOG_FILE_MAX_BYTES,
            backupCount=DEFAULT_LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as err:
        logger.error(
            "log file %s could not be opened (%s); logging to the terminal only", path, err
        )
        return None

    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FORMAT))
    # Held at its own level: /loglevel and OPENSRE_LOG_LEVEL steer the terminal.
    setattr(handler, FIXED_LEVEL_ATTR, True)
    _lower_root_for(root, level)
    root.addHandler(handler)
    return path


def _lower_root_for(root: logging.Logger, level: int) -> None:
    """Let ``level`` through the root logger without widening other handlers.

    The root filters before any handler, so it has to admit what the file wants
    or the file stays empty. A handler left at NOTSET inherits whatever the root
    allows, though, so pin those to the floor that was in force first — an
    embedding host's console must not start emitting DEBUG because a file asked
    for it.
    """
    previous_floor = root.getEffectiveLevel()
    if previous_floor <= level:
        return
    for handler in root.handlers:
        if handler.level == logging.NOTSET:
            handler.setLevel(previous_floor)
    root.setLevel(level)


__all__ = [
    "DEFAULT_LOG_FILE_LEVEL",
    "install_file_log_handler",
    "installed_log_file",
    "log_file_level",
    "log_file_path",
]
