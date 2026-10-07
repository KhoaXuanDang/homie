from collections import deque
from threading import Lock
from time import monotonic
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import TypeAdapter, ValidationError

from app.config import settings
from app.models.user import UserSub

bearer = HTTPBearer(auto_error=False)


class RateLimiter:
    def __init__(self) -> None:
        self.requests: dict[str, deque[float]] = {}
        self.lock = Lock()

    def check(self, user_sub: str) -> None:
        with self.lock:
            now = monotonic()
            if user_sub not in self.requests:
                for sub in list(self.requests):
                    if self.requests[sub][-1] <= now - 60:
                        del self.requests[sub]
                if len(self.requests) >= 10000:
                    raise HTTPException(
                        429,
                        "Too many requests. Please try again in a minute.",
                        headers={"Retry-After": "60"},
                    )
                self.requests[user_sub] = deque()
            timestamps = self.requests[user_sub]
            while timestamps and timestamps[0] <= now - 60:
                timestamps.popleft()
            if len(timestamps) >= 120:
                raise HTTPException(
                    429,
                    "Too many requests. Please try again in a minute.",
                    headers={"Retry-After": "60"},
                )
            timestamps.append(now)


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
    try:
        user_sub = TypeAdapter(UserSub).validate_python(credentials.credentials)
        if any(character.isspace() for character in user_sub):
            raise ValueError("Invalid token")
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            401, "Invalid authentication token.", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    request.state.user_sub = user_sub
    request.app.state.rate_limiter.check(user_sub)
    return user_sub
