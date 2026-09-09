"""Minimal logging: console + workdir/voicecut.log.

Configured once at startup; other modules use get_logger().
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_logger: logging.Logger | None = None

# Background worker threads can outlive pytest's captured stderr; do not let a
# handler failure (e.g. writing to a closed stream) surface as a traceback.
logging.raiseExceptions = False


def setup_logging(
    workdir: str | Path | None = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure the 'voicecut' logger (idempotent)."""
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger("voicecut")
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        if workdir:
            p = Path(workdir)
            p.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(p / "voicecut.log", encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
    _logger = logger
    return logger


def get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        _logger = setup_logging()
    return _logger