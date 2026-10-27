"""Activity log: a plain-text record of what CrateTag did, for troubleshooting.

Written to ~/Library/Logs/CrateTag/activity.log (rotated at 5 MB, 3 old files kept).
Open it from Help > Activity Log… in the app. Safe to call from background threads.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

_logger: logging.Logger | None = None
_path: Path | None = None


def log_dir() -> Path:
    override = os.environ.get("CRATETAG_LOG_DIR")
    if override:
        d = Path(override)
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Logs" / "CrateTag"
    else:
        d = Path.home() / ".cratetag" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    return _path or (log_dir() / "activity.log")


def _get() -> logging.Logger:
    global _logger, _path
    if _logger is not None:
        return _logger
    lg = logging.getLogger("cratetag.activity")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    try:
        _path = log_dir() / "activity.log"
        h = RotatingFileHandler(_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        lg.addHandler(h)
    except OSError:
        lg.addHandler(logging.NullHandler())  # never let logging break the app
    _logger = lg
    return lg


def reset() -> None:
    """Close the log file (used by tests that point CRATETAG_LOG_DIR somewhere new)."""
    global _logger, _path
    if _logger is not None:
        for h in list(_logger.handlers):
            h.close()
            _logger.removeHandler(h)
    _logger, _path = None, None


def info(msg: str) -> None:
    try:
        _get().info(msg)
    except Exception:  # noqa: BLE001
        pass


def warn(msg: str) -> None:
    try:
        _get().warning(msg)
    except Exception:  # noqa: BLE001
        pass


def error(msg: str) -> None:
    try:
        _get().error(msg)
    except Exception:  # noqa: BLE001
        pass


def q(value) -> str:
    """Show a value in the log: quoted, with blanks made visible."""
    if value is None:
        return "(unchanged)"
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(v) for v in value)
    value = str(value)
    return f"'{value}'" if value else "(blank)"


def change(field: str, old, new) -> str:
    return f"{field} {q(old)} → {q(new)}"


def install_crash_logging() -> None:
    """Record unexpected errors (with details) in the activity log, then carry on as normal."""
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        error("Unexpected error:\n" + "".join(traceback.format_exception(exc_type, exc, tb)).rstrip())
        previous(exc_type, exc, tb)

    sys.excepthook = hook


def tail(lines: int = 3000) -> str:
    """The last lines of the log, newest at the bottom."""
    p = log_path()
    try:
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 2_000_000))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])
