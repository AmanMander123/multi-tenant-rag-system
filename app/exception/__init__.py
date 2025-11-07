"""Application exception helpers."""

from .custom_exception import (
    AppException,
    ClientException,
    ServerException,
    UnsupportedDocumentTypeError,
    app_exception_handler,
    register_exception_handlers,
)

__all__ = [
    "AppException",
    "ClientException",
    "ServerException",
    "UnsupportedDocumentTypeError",
    "app_exception_handler",
    "register_exception_handlers",
]
