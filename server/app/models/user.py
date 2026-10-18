from datetime import datetime
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints, model_validator

UserSub = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
TravelMode = Literal["DRIVE", "WALK", "BICYCLE", "TRANSIT"]
Address = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Use a valid IANA timezone") from exc
    return value


Timezone = Annotated[str, AfterValidator(_validate_timezone)]


class Home(BaseModel):
    address: str
    lat: float | None = None
    lng: float | None = None


class HomeInput(BaseModel):
    """Clients send only the address; the server owns the coordinates."""

    model_config = ConfigDict(extra="forbid")

    address: Address


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    timezone: Timezone = "UTC"
    home: Home | None = None
    travel_mode: TravelMode = "DRIVE"


class ProfileUpdate(BaseModel):
    """Omitted fields keep their stored value; `home: null` clears the home address."""

    model_config = ConfigDict(extra="forbid")

    timezone: Timezone | None = None
    home: HomeInput | None = None
    travel_mode: TravelMode | None = None

    @model_validator(mode="after")
    def validate_updates(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        if any(getattr(self, f) is None for f in self.model_fields_set if f != "home"):
            raise ValueError("Update fields cannot be null")
        return self


class User(Profile):
    id: str
    user_sub: UserSub
    created_at: datetime
    updated_at: datetime
