from collections import OrderedDict, deque
from threading import Lock
from time import monotonic
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import TypeAdapter, ValidationError

from app.config import settings
from app.models.user import UserSub

bearer = HTTPBearer(auto_error=False)
_user_sub_adapter = TypeAdapter(UserSub)


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


def get_user_sub(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if credentials is None:
        raise HTTPException(
            401, "Provide an Authorization header.", headers={"WWW-Authenticate": "Bearer"}
        )
    if settings.jwt_secret:
        # Real Alexa OAuth/JWT verification is a later phase; never accept mock tokens here.
        raise HTTPException(503, "Alexa token validation is not configured.")
    if not settings.allow_mock_auth:
        raise HTTPException(503, "Authentication is not configured.")
    try:
        user_sub = _user_sub_adapter.validate_python(credentials.credentials)
        if any(character.isspace() for character in user_sub):
            raise ValueError("Invalid token")
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            401, "Invalid authentication token.", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    request.state.user_sub = user_sub
    request.app.state.rate_limiter.check(user_sub)
    return user_sub
