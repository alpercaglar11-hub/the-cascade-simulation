"""Standalone logger for paper_trading — plain stdlib logging with structlog-like call signature."""

import logging
import sys


def _setup():
    """Configure plain stdlib logging."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"
        )
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


_setup()


def get_logger(name: str):
    """Return a logger that accepts extra kwargs like structlog (ignored by stdlib)."""
    lg = logging.getLogger(name)
    # Attach a proxy that swallows unexpected keyword args
    return _StdlibLogProxy(lg)


class _StdlibLogProxy:
    """Wraps stdlib logger to accept both positional-args and kwargs-style calls.

    In tests the code calls e.g. log.info("event", key=value).
    stdlib logging.Logger.info() raises on unexpected keyword arguments,
    so we intercept and discard extras.
    """

    __slots__ = ("_lg",)

    def __init__(self, logger: logging.Logger):
        self._lg = logger

    def _emit(self, level: int, msg: str, args, kwargs):
        extras = {
            k: v
            for k, v in kwargs.items()
            if k not in ("exc_info", "stack_info", "stacklevel", "extra")
        }
        self._lg.log(level, msg, *args, extra=extras)

    def debug(self, msg, *args, **kwargs):
        self._emit(logging.DEBUG, msg, args, kwargs)

    def info(self, msg, *args, **kwargs):
        self._emit(logging.INFO, msg, args, kwargs)

    def warning(self, msg, *args, **kwargs):
        self._emit(logging.WARNING, msg, args, kwargs)

    def error(self, msg, *args, **kwargs):
        self._emit(logging.ERROR, msg, args, kwargs)

    def critical(self, msg, *args, **kwargs):
        self._emit(logging.CRITICAL, msg, args, kwargs)

    def audit(self, msg, *args, **kwargs):
        """Audit-level: maps to INFO with [AUDIT] prefix."""
        self._lg.info(f"[AUDIT] {msg}", *args)

    @property
    def name(self):
        return self._lg.name
