"""Logging setup (loguru).

One stderr sink for the console and one rotating file sink under
``<data_dir>/logs``.  Callers use ``logger.info("[BTC] ...")`` rather than
``bind`` so a log line stays readable when it is copied out of context.
"""
from __future__ import annotations

import os
import sys

from loguru import logger

_CONFIGURED = False


def configure_logging(level: str = "INFO", data_dir: str = "data") -> None:
    """Configure loguru once.  Safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <7}</level> | <level>{message}</level>"
        ),
        backtrace=False,
        diagnose=False,
    )

    log_dir = os.path.join(data_dir, "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        logger.add(
            os.path.join(log_dir, "bot_{time:YYYYMMDD}.log"),
            level=level.upper(),
            rotation="00:00",
            retention="14 days",
            encoding="utf-8",
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )
    except OSError:
        # A read-only data dir must not prevent the bot from starting.
        logger.warning("could not open file log sink at {}", log_dir)


def get_logger(name: str = "poly5m"):
    return logger
