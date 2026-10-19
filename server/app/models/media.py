from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.memory import Tags

ContentType = Literal[
    "image/jpeg", "image/png", "image/webp", "image/heic", "video/mp4", "video/quicktime"
]
Caption = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class MediaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: ContentType
    size: Annotated[int, Field(strict=True, gt=0)]
    caption: Caption = ""

    @model_validator(mode="after")
    def validate_size(self) -> Self:
        cap = 500 if self.content_type.startswith("video/") else 50
        if self.size > cap * 1024 * 1024:
            raise ValueError(f"Keep uploads under {cap} MB")
        return self


class MediaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caption: Caption | None = None
    tags: Tags | None = None
    memory_id: UUID | None = None
    training_consent: Annotated[bool, Field(strict=True)] | None = None

    @model_validator(mode="after")
    def validate_updates(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        if any(
            getattr(self, field) is None for field in self.model_fields_set if field != "memory_id"
        ):
            raise ValueError("Update fields cannot be null")
        return self


class Media(BaseModel):
    id: UUID
    content_type: ContentType
    size: int
    caption: str
    tags: list[str]
    memory_id: UUID | None = None
    training_consent: bool
    status: Literal["pending", "uploaded"]
    url: str
    created_at: datetime


class MediaUpload(BaseModel):
    media: Media
    upload_url: str
    upload_headers: dict[str, str]
    expires_in: int
