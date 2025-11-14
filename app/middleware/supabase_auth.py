from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx
from fastapi import Request
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AuthContext:
    subject: str
    tenant_id: str
    email: Optional[str] = None
    raw_claims: Dict[str, Any] | None = None


class SupabaseJWKSClient:
    """Caches and serves JWKS keys for Supabase-issued JWTs."""

    def __init__(self, jwks_url: str, cache_ttl_seconds: int = 600) -> None:
        self.jwks_url = jwks_url
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self._jwks: Dict[str, Any] | None = None
        self._expiry = datetime.now(timezone.utc)
        self._lock = asyncio.Lock()

    async def get_key(self, kid: str) -> Dict[str, Any]:
        await self._ensure_fresh_keys()
        assert self._jwks is not None
        for key in self._jwks.get("keys", []):
            if key.get("kid") == kid:
                return key
        raise JWTError("No matching JWKS key found for Supabase token.")

    async def _ensure_fresh_keys(self) -> None:
        async with self._lock:
            if self._jwks and datetime.now(timezone.utc) < self._expiry:
                return
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(self.jwks_url)
                response.raise_for_status()
                self._jwks = response.json()
                self._expiry = datetime.now(timezone.utc) + self.cache_ttl
                logger.info("Refreshed Supabase JWKS.", extra={"jwks_url": self.jwks_url})


class SupabaseAuthMiddleware(BaseHTTPMiddleware):
    """Best-effort Supabase JWT validation that decorates request.state.auth_context."""

    def __init__(self, app, *, enforce_auth: bool = False) -> None:  # type: ignore[override]
        super().__init__(app)
        self.settings = get_settings()
        self.enforce_auth = enforce_auth or self.settings.supabase_auth_required
        self.jwks_client = SupabaseJWKSClient(self.settings.supabase_jwks_url)

    async def dispatch(self, request: Request, call_next):
        token = self._extract_token(request)

        if not token:
            if self.enforce_auth:
                return JSONResponse(
                    {"detail": "authorization_required"},
                    status_code=401,
                )
            return await call_next(request)

        try:
            claims = await self._decode_token(token)
            auth_context = AuthContext(
                subject=str(claims.get("sub")),
                tenant_id=self._resolve_tenant_id(claims),
                email=claims.get("email"),
                raw_claims=claims,
            )
            request.state.auth_context = auth_context
        except JWTError as exc:
            logger.warning("Failed to validate Supabase token: %s", exc)
            if self.enforce_auth:
                return JSONResponse(
                    {"detail": "invalid_token"},
                    status_code=401,
                )

        response = await call_next(request)
        return response

    def _extract_token(self, request: Request) -> Optional[str]:
        header = request.headers.get("Authorization")
        if not header:
            return None
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return value.strip()

    async def _decode_token(self, token: str) -> Dict[str, Any]:
        headers = jwt.get_unverified_header(token)
        kid = headers.get("kid")
        if not kid:
            raise JWTError("Supabase token missing 'kid' header.")
        key = await self.jwks_client.get_key(kid)
        return jwt.decode(
            token,
            key,
            algorithms=[key.get("alg", "RS256")],
            audience=self.settings.supabase_jwt_audience,
        )

    def _resolve_tenant_id(self, claims: Dict[str, Any]) -> str:
        app_metadata = claims.get("app_metadata") or {}
        user_metadata = claims.get("user_metadata") or {}
        return (
            str(app_metadata.get("tenant_id"))
            or str(user_metadata.get("tenant_id"))
            or self.settings.default_tenant_id
        )
