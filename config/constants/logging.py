"""Env-var names for runtime log verbosity and the optional log file."""

from __future__ import annotations

OPENSRE_LOG_LEVEL_ENV = "OPENSRE_LOG_LEVEL"

#: Path of the durable log sink. Unset means no file is written at all.
OPENSRE_LOG_FILE_ENV = "OPENSRE_LOG_FILE"

#: The file's own floor, held independently of the terminal's so a quiet shell
#: can still record everything on disk.
OPENSRE_LOG_FILE_LEVEL_ENV = "OPENSRE_LOG_FILE_LEVEL"

DEFAULT_LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_FILE_BACKUP_COUNT = 3

__all__ = [
    "DEFAULT_LOG_FILE_BACKUP_COUNT",
    "DEFAULT_LOG_FILE_MAX_BYTES",
    "OPENSRE_LOG_FILE_ENV",
    "OPENSRE_LOG_FILE_LEVEL_ENV",
    "OPENSRE_LOG_LEVEL_ENV",
]
