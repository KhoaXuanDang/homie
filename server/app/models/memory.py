from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Kind = Literal["item_location", "fact", "preference", "todo"]
Content = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Tag = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=40)
]


def _normalize_tags(tags: list[str]) -> list[str]:
    return list(dict.fromkeys(tags))


Tags = Annotated[list[Tag], Field(max_length=10), AfterValidator(_normalize_tags)]
EventId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)]
Limit = Annotated[int, Field(ge=1, le=200)]


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Content
    kind: Kind
    tags: Tags = Field(default_factory=list)
    event_id: EventId | None = None


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Content | None = None
    kind: Kind | None = None
    tags: Tags | None = None
    event_id: EventId | None = None

    @model_validator(mode="after")
    def validate_updates(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        if any(
            getattr(self, field) is None for field in self.model_fields_set if field != "event_id"
        ):
            raise ValueError("Update fields cannot be null")
        return self


class Memory(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_sub: str
    content: str
    kind: Kind
    tags: list[str]
    event_id: str | None = None
    media_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    timezone: str = "UTC"
