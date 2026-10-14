import asyncio
import logging
from collections import OrderedDict, deque
from threading import Lock
from time import monotonic
from typing import Annotated, Any

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ValidationError
from starlette.responses import JSONResponse

from app.config import settings
from app.models.user import UserSub

logger = logging.getLogger(__name__)
JWKS_CACHE_TTL_SECONDS = 300.0
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_cache_lock = Lock()


class CurrentUser(BaseModel):
    sub: UserSub
    client_id: str
    scope: frozenset[str]
    claims: dict[str, Any]


def _load_jwks(jwks_url: str, *, force_refresh: bool = False) -> dict[str, Any]:
    now = monotonic()
    with _jwks_cache_lock:
        cached = _jwks_cache.get(jwks_url)
        if not force_refresh and cached is not None and now - cached[0] < JWKS_CACHE_TTL_SECONDS:
            return cached[1]

        response = httpx.get(jwks_url, timeout=5.0)
        response.raise_for_status()
        jwks = response.json()
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise ValueError("Invalid JWKS document")
        _jwks_cache[jwks_url] = (now, jwks)
        return jwks


def verify_token(token: str, required_scope: str) -> CurrentUser:
    """Verify a Cognito access token and return its authenticated principal."""
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str):
            raise ValueError("Unsupported JWT signing key")

        jwks_url = settings.cognito_jwks_url
        jwks = _load_jwks(jwks_url)

        def find_signing_key(document: dict[str, Any]) -> dict[str, Any] | None:
            return next(
                (
                    key
                    for key in document["keys"]
                    if isinstance(key, dict) and key.get("kid") == kid and key.get("kty") == "RSA"
                ),
                None,
            )

        jwk = find_signing_key(jwks)
        if jwk is None:
            jwk = find_signing_key(_load_jwks(jwks_url, force_refresh=True))
        if jwk is None:
            raise ValueError("JWT signing key was not found")

        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(jwk).key,
            algorithms=["RS256"],
            issuer=settings.cognito_issuer,
            options={
                "require": ["iss", "exp", "sub", "token_use", "client_id", "scope"],
                "verify_aud": False,
            },
        )
        if claims.get("token_use") != "access":
            raise ValueError("Expected a Cognito access token")
        client_id = claims.get("client_id")
        if not isinstance(client_id, str) or client_id not in settings.cognito_client_ids:
            raise ValueError("JWT client is not allowed")
        raw_scope = claims.get("scope")
        if not isinstance(raw_scope, str):
            raise ValueError("JWT scopes are invalid")
        scopes = frozenset(raw_scope.split())
        if required_scope not in scopes:
            raise ValueError("Required JWT scope is missing")

        return CurrentUser(
            sub=claims["sub"],
            client_id=client_id,
            scope=scopes,
            claims=claims,
        )
    except (
        jwt.PyJWTError,
        httpx.HTTPError,
        KeyError,
        StopIteration,
        TypeError,
        ValidationError,
        ValueError,
    ) as exc:
        logger.info("Cognito access token rejected (%s)", type(exc).__name__)
        raise ValueError("Invalid authentication token") from exc


def _unauthorized() -> HTTPException:
    return HTTPException(401, "Invalid or missing authentication token.")


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip() or " " in token.strip():
        raise _unauthorized()
    return token.strip()


class RateLimiter:
    def __init__(
        self,
        max_requests: int = 120,
        window_seconds: float = 60.0,
        max_entries: int = 10000,
        cleanup_interval: float = 10.0,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_entries = max_entries
        self.cleanup_interval = cleanup_interval
        self.requests: OrderedDict[str, deque[float]] = OrderedDict()
        self.last_cleanup = 0.0
        self.lock = Lock()

    def _cleanup_expired(self, now: float) -> None:
        self.last_cleanup = now
        cutoff = now - self.window_seconds
        while self.requests:
            sub, timestamps = next(iter(self.requests.items()))
            if timestamps and timestamps[-1] > cutoff:
                break
            self.requests.popitem(last=False)

    def check(self, user_sub: str) -> None:
        with self.lock:
            now = monotonic()
            if now - self.last_cleanup >= self.cleanup_interval:
                self._cleanup_expired(now)

            if user_sub not in self.requests:
                if len(self.requests) >= self.max_entries:
                    self._cleanup_expired(now)
                    while len(self.requests) >= self.max_entries:
                        self.requests.popitem(last=False)
                self.requests[user_sub] = deque()

            timestamps = self.requests[user_sub]
            while timestamps and timestamps[0] <= now - self.window_seconds:
                timestamps.popleft()

            if len(timestamps) >= self.max_requests:
                raise HTTPException(
                    429,
                    "Too many requests. Please try again in a minute.",
                    headers={"Retry-After": "60"},
                )

            timestamps.append(now)
            self.requests.move_to_end(user_sub)


def current_user(request: Request) -> CurrentUser:
    try:
        user = verify_token(_bearer_token(request), settings.mcp_tools_scope)
    except HTTPException:
        raise
    except ValueError as exc:
        raise _unauthorized() from exc
    request.state.user_sub = user.sub
    request.app.state.api_rate_limiter.check(user.sub)
    return user


CurrentUserDependency = Annotated[CurrentUser, Depends(current_user)]


class McpAuthGate:
    """Authenticate MCP requests with the service scope before routing them."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope.get("type") != "http" or not (path == "/mcp" or path.startswith("/mcp/")):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        try:
            token = _bearer_token(request)
            user = await asyncio.to_thread(verify_token, token, settings.mcp_service_scope)
            scope.setdefault("state", {})["user_sub"] = user.sub
            request.app.state.mcp_rate_limiter.check(user.sub)
        except ValueError:
            response = JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing authentication token."},
            )
            await response(scope, receive, send)
            return
        except HTTPException as exc:
            response = JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
