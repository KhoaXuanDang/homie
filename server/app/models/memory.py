from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Content = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
MemoryType = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Tags = Annotated[list[Tag], Field(max_length=20)]


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Content
    memory_type: MemoryType
    tags: Tags = Field(default_factory=list)


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Content | None = None
    memory_type: MemoryType | None = None
    tags: Tags | None = None

    @model_validator(mode="after")
    def validate_updates(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Update fields cannot be null")
        return self


class Memory(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_sub: str
    content: str
    memory_type: str
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    timezone: str = "UTC"
