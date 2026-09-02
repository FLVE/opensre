"""A durable log sink, for the detail a terminal scrollback cannot keep.

The shell's handler prints and forgets. Nothing in the product writes a log
file, so a run that misbehaved once leaves nothing to read afterwards.
"""

from __future__ import annotations

import logging
import os
import stat
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from infrastructure.logging import (
    ShellLogHandler,
    install_file_log_handler,
    install_shell_log_handler,
    installed_log_file,
    set_log_level,
)


@pytest.fixture(autouse=True)
def _restore_root():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in original_handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in original_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(original_level)


def _bare_root() -> logging.Logger:
    """Strip the root logger, as the shell finds it at startup.

    ``install_shell_log_handler`` skips a root that already has handlers, and
    pytest's logging plugin re-attaches its own after fixtures run — so this
    has to be called from the test body or the test measures the plugin.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    return root


def test_no_file_is_written_until_the_operator_names_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in: an unset path must not create a file anywhere."""
    monkeypatch.delenv("OPENSRE_LOG_FILE", raising=False)

    assert install_file_log_handler() is None


def test_records_reach_the_named_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "opensre.log"
    assert install_file_log_handler(target) == target
    logging.getLogger("probe").debug("mimir query promql=up")

    assert "mimir query promql=up" in target.read_text(encoding="utf-8")


def test_the_file_records_the_time_level_and_logger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file has none of a terminal's context: bare messages are unreadable."""
    target = tmp_path / "opensre.log"
    install_file_log_handler(target)

    logging.getLogger("integrations.grafana.mimir").warning("query failed")

    line = target.read_text(encoding="utf-8").strip()
    assert "WARNING" in line
    assert "integrations.grafana.mimir" in line


def test_a_quiet_terminal_still_gets_a_full_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The point of the file: DEBUG on disk while the shell shows only errors.

    ``set_log_level`` raises every handler it owns, and the root logger filters
    before any of them, so both have to leave the file's floor alone.
    """
    target = tmp_path / "opensre.log"
    monkeypatch.setenv("OPENSRE_LOG_FILE_LEVEL", "DEBUG")
    install_file_log_handler(target)

    set_log_level(logging.ERROR)
    logging.getLogger("probe").debug("still on disk")

    assert "still on disk" in target.read_text(encoding="utf-8")


def test_an_unwritable_path_does_not_stop_the_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A full disk or a read-only home must not keep the shell from starting."""
    monkeypatch.setattr(Path, "mkdir", _raise_permission_error)

    assert install_file_log_handler(tmp_path / "missing" / "x" / "no.log") is None


def _raise_permission_error(*_args: object, **_kwargs: object) -> None:
    raise PermissionError("read-only filesystem")


def test_the_shell_handler_survives_the_file_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``install_shell_log_handler`` skips a root that already has handlers, so
    installing the file sink first would silently leave the terminal bare.
    """
    root = _bare_root()

    install_shell_log_handler()
    install_file_log_handler(tmp_path / "opensre.log")

    assert any(isinstance(handler, ShellLogHandler) for handler in root.handlers)


def test_the_file_is_readable_only_by_its_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It carries tool arguments and outbound URLs; the default umask would
    leave it 0644 for every other account on the machine.
    """
    target = tmp_path / "opensre.log"
    install_file_log_handler(target)

    logging.getLogger("probe").debug("Authorization: Bearer sk-live-1")

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_failure_to_open_reaches_a_default_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The shell handler is installed at ERROR one line earlier, so a WARNING
    about the missing file is discarded and the loss is silent.
    """
    monkeypatch.setattr(Path, "mkdir", _raise_permission_error)
    caplog.set_level(logging.DEBUG, logger="infrastructure.logging.file_sink")

    assert install_file_log_handler(tmp_path / "nope" / "x.log") is None

    assert [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_a_home_that_cannot_be_resolved_does_not_abort_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Path.expanduser`` raises for an unknown ``~user``; that happened
    outside the installer's guard and took the shell down with it.
    """
    monkeypatch.setenv("OPENSRE_LOG_FILE", "~nonexistent-user-xyz/debug.log")
    monkeypatch.setattr(Path, "expanduser", _raise_runtime_error)

    assert install_file_log_handler() is None


def _raise_runtime_error(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("could not determine home directory")


def test_an_unrelated_rotating_file_handler_is_not_mistaken_for_this_sink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A host with its own rotating file handler would otherwise suppress the
    install and still be reported as writing the requested path.
    """
    target = tmp_path / "opensre.log"
    root = _bare_root()
    root.addHandler(RotatingFileHandler(tmp_path / "host.log"))

    assert install_file_log_handler(target) == target
    assert installed_log_file() == target


def test_nothing_is_reported_as_installed_when_the_open_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``/loglevel`` reads this; reading the environment instead would claim a
    file was being written when the open failed.
    """
    monkeypatch.setattr(Path, "mkdir", _raise_permission_error)
    install_file_log_handler(tmp_path / "nope" / "x.log")

    assert installed_log_file() is None


def test_a_host_handler_keeps_its_floor_when_the_file_lowers_the_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A host handler left at NOTSET inherits whatever the root allows, so
    lowering the root for the file would start emitting DEBUG through it.
    """
    monkeypatch.setenv("OPENSRE_LOG_FILE_LEVEL", "DEBUG")
    root = _bare_root()
    root.setLevel(logging.WARNING)
    host = logging.NullHandler()
    root.addHandler(host)

    install_file_log_handler(tmp_path / "opensre.log")

    assert host.level == logging.WARNING


def test_a_write_failure_disables_the_sink_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A disk that fills after the file opened is past the installer's guard;
    stdlib would then drop records silently or print a traceback per record.
    """
    target = tmp_path / "opensre.log"
    install_file_log_handler(target)
    sink = next(h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler))
    # Inside RotatingFileHandler.emit's own try, which is what calls handleError.
    monkeypatch.setattr(type(sink), "shouldRollover", _raise_os_error)
    caplog.set_level(logging.DEBUG, logger="infrastructure.logging.file_sink")

    logging.getLogger("probe").error("first")
    logging.getLogger("probe").error("second")

    complaints = [r for r in caplog.records if "log file" in r.getMessage()]
    assert len(complaints) == 1


def _raise_os_error(*_args: object, **_kwargs: object) -> None:
    raise OSError("No space left on device")


def test_a_configured_file_is_left_alone_during_a_test_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A developer with OPENSRE_LOG_FILE in their .env had every pytest session
    open that file, write into their working tree, and leave the handler behind
    for whatever test ran next.
    """
    configured = tmp_path / "from_dot_env.log"
    monkeypatch.setenv("OPENSRE_LOG_FILE", str(configured))

    assert install_file_log_handler() is None
    assert not configured.exists()


def test_a_file_whose_permissions_cannot_be_tightened_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pre-existing file owned by someone else stays group/world readable:
    ``os.open`` succeeds to append and only the chmod fails. Writing DEBUG tool
    arguments into it would break the owner-only contract silently.
    """
    target = tmp_path / "shared.log"
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(os, "fchmod", _raise_permission_error)

    assert install_file_log_handler(target) is None
    assert installed_log_file() is None


def test_a_disabled_sink_is_no_longer_reported_as_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``/loglevel`` would otherwise keep naming a file that stopped accepting
    records after the disk filled.
    """
    target = tmp_path / "opensre.log"
    install_file_log_handler(target)
    sink = next(h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler))
    monkeypatch.setattr(type(sink), "shouldRollover", _raise_os_error)

    logging.getLogger("probe").error("fills the disk")

    assert installed_log_file() is None
