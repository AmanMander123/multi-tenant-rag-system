"""
Application logging utilities.

This module centralises logging configuration so that the rest of the codebase
can retrieve pre-configured loggers via ``get_logger`` and attach contextual
metadata that travels with every log entry.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.config
import logging.handlers
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from inspect import iscoroutinefunction
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | %(request_id)s | %(message)s"
)
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_LOG_RECORD_RESERVED_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}

APP_LOG_LEVEL = "APP_LOG_LEVEL"
APP_LOG_FORMAT = "APP_LOG_FORMAT"
APP_LOG_DATEFMT = "APP_LOG_DATEFMT"
APP_LOG_FILE = "APP_LOG_FILE"
APP_LOG_DISABLE_CONSOLE = "APP_LOG_DISABLE_CONSOLE"
APP_LOG_JSON = "APP_LOG_JSON"

_context_data: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "logger_context", default={}
)
_configured = False


class ContextFilter(logging.Filter):
    """Populate log records with correlation context values."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = {
            key: value
            for key, value in _context_data.get({}).items()
            if key not in _LOG_RECORD_RESERVED_ATTRS
        }
        for key, value in context.items():
            setattr(record, key, value)
        # Provide defaults so format strings that reference these fields never fail.
        if not hasattr(record, "request_id"):
            record.request_id = context.get("request_id", "-")
        return True


def _json_default(value: Any) -> Any:
    """
    Fallback JSON serializer for non-serialisable objects.

    The value is coerced to a string to ensure we never raise during logging.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


class JsonFormatter(logging.Formatter):
    """Emit JSON structured logs compatible with Cloud Run / GCP logging."""

    def format(self, record: logging.LogRecord) -> str:
        log_payload: Dict[str, Any] = {
            "time": self.formatTime(record, self.datefmt),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            log_payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            log_payload["stack_info"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key in _LOG_RECORD_RESERVED_ATTRS or key in {"exc_info", "stack_info"}:
                continue
            log_payload[key] = value

        return json.dumps(log_payload, default=_json_default, separators=(",", ":"))


@dataclass
class LoggerConfig:
    """
    Configuration payload for the logging system.

    Customise the overall logging behaviour via code or environment variables.
    The configuration can easily be extended with additional handlers or
    formatters as the project evolves.
    """

    level: str = field(default=DEFAULT_LOG_LEVEL)
    fmt: str = field(default=DEFAULT_LOG_FORMAT)
    datefmt: str = field(default=DEFAULT_DATE_FORMAT)
    console: bool = field(default=True)
    log_file: Optional[Path] = field(default=None)
    max_bytes: int = field(default=5 * 1024 * 1024)  # 5 MiB
    backup_count: int = field(default=3)
    disable_existing: bool = field(default=False)
    json_logs: bool = field(default=False)

    @classmethod
    def from_env(cls) -> "LoggerConfig":
        """Create a configuration instance populated from environment variables."""
        config = cls()

        level = os.getenv(APP_LOG_LEVEL)
        if level:
            config.level = level.upper()

        log_format = os.getenv(APP_LOG_FORMAT)
        if log_format:
            config.fmt = log_format

        date_format = os.getenv(APP_LOG_DATEFMT)
        if date_format:
            config.datefmt = date_format

        file_path = os.getenv(APP_LOG_FILE)
        if file_path:
            config.log_file = Path(file_path)

        disable_console = os.getenv(APP_LOG_DISABLE_CONSOLE)
        if disable_console is not None:
            config.console = disable_console not in {"1", "true", "True"}

        json_logs = os.getenv(APP_LOG_JSON)
        if json_logs is not None:
            config.json_logs = json_logs in {"1", "true", "True"}

        return config

    def _ensure_log_directory(self) -> None:
        """Create parent directories for the log file if necessary."""
        if not self.log_file:
            return
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Directory creation might fail in read-only environments. In that
            # case we fall back to console logging only.
            self.log_file = None

    def to_dict(self) -> Dict[str, Any]:
        """
        Translate this config into a dictConfig payload.

        Returns:
            A dictionary that can be passed to ``logging.config.dictConfig``.
        """
        self._ensure_log_directory()

        filters = {
            "context": {
                "()": "app.logger.custom_logger.ContextFilter",
            }
        }

        if self.json_logs:
            formatter_config: Dict[str, Any] = {
                "class": "app.logger.custom_logger.JsonFormatter",
                "datefmt": self.datefmt,
            }
        else:
            formatter_config = {
                "format": self.fmt,
                "datefmt": self.datefmt,
            }

        formatters = {
            "structured": formatter_config,
        }

        handlers: Dict[str, Dict[str, Any]] = {}
        handler_names: list[str] = []

        if self.console:
            handlers["console"] = {
                "class": "logging.StreamHandler",
                "level": self.level,
                "formatter": "structured",
                "filters": ["context"],
            }
            handler_names = [*handler_names, "console"]

        if self.log_file:
            handlers["rotating_file"] = {
                "class": "logging.handlers.RotatingFileHandler",
                "level": self.level,
                "formatter": "structured",
                "filename": str(self.log_file),
                "maxBytes": self.max_bytes,
                "backupCount": self.backup_count,
                "encoding": "utf-8",
                "filters": ["context"],
            }
            handler_names = [*handler_names, "rotating_file"]

        if not handler_names:
            # Prevent misconfiguration from disabling all logging.
            handlers["console"] = {
                "class": "logging.StreamHandler",
                "level": self.level,
                "formatter": "structured",
                "filters": ["context"],
            }
            handler_names = ["console"]

        return {
            "version": 1,
            "disable_existing_loggers": self.disable_existing,
            "filters": filters,
            "formatters": formatters,
            "handlers": handlers,
            "root": {
                "level": self.level,
                "handlers": list(handler_names),
            },
        }


def configure_logging(config: Optional[LoggerConfig] = None, force: bool = False) -> None:
    """
    Initialise the logging system for the application.

    Args:
        config: Optional LoggerConfig instance. If omitted, environment settings
            are consulted before falling back to sensible defaults.
        force: Reconfigure even if logging was already set up.
    """
    global _configured

    if _configured and not force:
        return

    config = config or LoggerConfig.from_env()
    logging.config.dictConfig(config.to_dict())
    logging.captureWarnings(True)
    _configured = True


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Retrieve a logger configured for the application.

    Args:
        name: Optional module or component name.

    Returns:
        A ``logging.Logger`` instance ready for use.
    """
    if not _configured:
        configure_logging()
    return logging.getLogger(name)


def bind_context(**kwargs: Any) -> None:
    """Attach key/value pairs to the logging context."""
    current = dict(_context_data.get({}))
    for key, value in kwargs.items():
        if value is None:
            continue
        if key in _LOG_RECORD_RESERVED_ATTRS:
            continue
        current[key] = value
    _context_data.set(current)


def get_context() -> Dict[str, Any]:
    """Return a shallow copy of the current logging context."""
    return {
        key: value
        for key, value in _context_data.get({}).items()
        if key not in _LOG_RECORD_RESERVED_ATTRS
    }


def clear_context(*keys: str) -> None:
    """
    Remove contextual keys from the logging context.

    Args:
        *keys: Optional list of keys. If omitted, the entire context is cleared.
    """
    if not keys:
        _context_data.set({})
        return

    current = dict(_context_data.get({}))
    for key in keys:
        current.pop(key, None)
    _context_data.set(current)


@contextmanager
def log_context(**kwargs: Any):
    """
    Context manager to temporarily bind log context data.

    Example:

        with log_context(request_id="abc123"):
            logger.info("Correlated log entry")
    """
    scoped_values = {
        key: value
        for key, value in kwargs.items()
        if value is not None and key not in _LOG_RECORD_RESERVED_ATTRS
    }
    token = _context_data.set({**_context_data.get({}), **scoped_values})
    try:
        yield
    finally:
        _context_data.reset(token)


def preserve_context(func):
    """
    Capture the current logging context and apply it when ``func`` executes.

    This helper is useful when dispatching work to background tasks, thread
    pools, or other worker queues where ``contextvars`` propagation is not
    guaranteed (for example, FastAPI BackgroundTasks or ``asyncio.to_thread``).
    """
    captured_context = get_context()

    if iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            token = _context_data.set({**_context_data.get({}), **captured_context})
            try:
                return await func(*args, **kwargs)
            finally:
                _context_data.reset(token)

        return async_wrapper

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        token = _context_data.set({**_context_data.get({}), **captured_context})
        try:
            return func(*args, **kwargs)
        finally:
            _context_data.reset(token)

    return sync_wrapper
