"""Structured logging setup using structlog."""

import structlog
import logging
import sys
from config.settings import settings


def setup_logging() -> None:
    """Configure structured logging for the entire application."""

    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(serializer=orjson.dumps),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


class AuditLogger:
    """Audit-specific logger that always emits structured JSON with action metadata."""

    def __init__(self, name: str):
        self._log = structlog.get_logger(name)

    def info(self, action: str, **kwargs) -> None:
        self._log.info("audit", action=action, **kwargs)

    def warning(self, action: str, **kwargs) -> None:
        self._log.warning("audit", action=action, **kwargs)

    def error(self, action: str, **kwargs) -> None:
        self._log.error("audit", action=action, **kwargs)


def get_logger(name: str) -> structlog.BoundLogger:
    """Get a typed logger instance."""
    return structlog.get_logger(name)


def get_audit_logger(name: str = "audit") -> AuditLogger:
    """Get an audit logger instance for compliance logging."""
    return AuditLogger(name)