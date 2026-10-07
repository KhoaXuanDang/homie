from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

UserSub = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a valid IANA timezone") from exc
        return value


class User(Profile):
    id: str
    user_sub: UserSub
    created_at: datetime
    updated_at: datetime
