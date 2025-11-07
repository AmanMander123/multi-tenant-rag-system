"""
Application-wide exception hierarchy and utilities.

The classes and helpers defined here keep error handling consistent across the
codebase. They preserve contextual metadata (request IDs, tenant IDs, etc.),
log structured error details, and translate into HTTP-friendly responses. This
makes them well suited for the multi-tenant, asynchronous workloads described
for the platform.
"""

from __future__ import annotations

import logging
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any, Dict, Iterable, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.logger import get_context, get_logger


class AppException(Exception):
    """
    Base class for all application-defined exceptions.

    Args:
        message: Human-readable error description.
        code: Stable error code that clients can depend on.
        status_code: HTTP status code that best represents the error.
        detail: Optional structured detail payload.
        context: Additional diagnostic metadata that augments the logger context.
        log_level: Logging level name to use when the exception is logged.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "app_error",
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        log_level: str = "ERROR",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.detail = detail
        base_context = get_context()
        provided_context = context or {}
        self.context = {
            key: value
            for key, value in {**base_context, **provided_context}.items()
            if value is not None
        }
        self.log_level = log_level.upper()

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the exception into a structured dictionary."""
        payload: Dict[str, Any] = {
            "error": self.code,
            "message": self.message,
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        if self.context:
            payload["context"] = self.context
        return payload

    def to_http_exception(self) -> HTTPException:
        """Translate the exception into a FastAPI HTTPException."""
        return HTTPException(status_code=self.status_code, detail=self.to_dict())

    def log(self, logger: Optional[logging.Logger] = None) -> "AppException":
        """
        Emit the exception via the application logger.

        Returns:
            The exception instance, enabling fluent usage.
        """
        logger = logger or get_logger(__name__)
        level = getattr(logging, self.log_level, logging.ERROR)
        extra: Dict[str, Any] = {
            "error_code": self.code,
            "status_code": self.status_code,
            **self.context,
        }
        if self.detail is not None:
            extra["error_detail"] = self.detail
        logger.log(level, self.message, extra=extra)
        return self

    def enrich(self, **context: Any) -> "AppException":
        """Augment the exception with additional contextual metadata."""
        for key, value in context.items():
            if value is None:
                continue
            self.context[key] = value
        return self


class ClientException(AppException):
    """4xx-class errors triggered by client input."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "client_error",
        status_code: int = status.HTTP_400_BAD_REQUEST,
        log_level: str = "WARNING",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message,
            code=code,
            status_code=status_code,
            log_level=log_level,
            **kwargs,
        )


class ServerException(AppException):
    """5xx-class errors triggered by server conditions."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "server_error",
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        log_level: str = "ERROR",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message,
            code=code,
            status_code=status_code,
            log_level=log_level,
            **kwargs,
        )


class UnsupportedDocumentTypeError(ClientException):
    """Raised when an uploaded document's MIME type is not supported."""

    def __init__(
        self,
        *,
        content_type: str,
        supported_types: Iterable[str],
        detail: Optional[Dict[str, Any]] = None,
        message: str = "Unsupported document type submitted.",
        **kwargs: Any,
    ) -> None:
        merged_detail: Dict[str, Any] = {
            "content_type": content_type,
            "supported_types": sorted(set(supported_types)),
        }
        if detail:
            merged_detail.update(detail)
        super().__init__(
            message,
            code="unsupported_document_type",
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=merged_detail,
            **kwargs,
        )


def app_exception_handler(func):
    """
    Decorator that converts AppException instances into HTTP responses.

    When applied to FastAPI endpoint handlers or background jobs, it ensures
    consistent logging and response formatting whenever an AppException (or
    subclass) is raised.
    """

    if iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except AppException as exc:
                exc.log()
                raise exc.to_http_exception()

        return async_wrapper

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AppException as exc:
            exc.log()
            raise exc.to_http_exception()

    return sync_wrapper


def register_exception_handlers(app: FastAPI) -> None:
    """
    Register global FastAPI handlers for application exceptions.

    This ensures that any ``AppException`` raised by routes, dependencies, or
    background tasks is automatically logged and translated into a structured
    JSON response without requiring per-route decorators.
    """

    @app.exception_handler(AppException)
    async def _handle_app_exception(request: Request, exc: AppException):
        exc.enrich(path=str(request.url.path), method=request.method)
        exc.log()
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())
