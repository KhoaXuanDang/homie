from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import Field, TypeAdapter
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.db import MemoryRecord, UserRecord, utc_now
from app.models.memory import Memory, MemoryCreate, MemoryUpdate, Tags
from app.models.user import Profile, UserSub


class MemoryNotFoundError(Exception):
    pass


class MemoryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _validate_sub(self, user_sub: str) -> str:
        return TypeAdapter(UserSub).validate_python(user_sub)

    def _get_record(self, user_sub: str, memory_id: str) -> MemoryRecord:
        user_sub = self._validate_sub(user_sub)
        record = self.session.scalar(
            select(MemoryRecord).where(
                MemoryRecord.user_sub == user_sub, MemoryRecord.id == memory_id
            )
        )
        if record is None:
            raise MemoryNotFoundError("Memory not found")
        return record

    def _to_memory(self, record: MemoryRecord) -> Memory:
        user = self.session.scalar(select(UserRecord).where(UserRecord.user_sub == record.user_sub))
        timezone = Profile.model_validate(user).timezone
        memory = Memory.model_validate(record)
        return memory.model_copy(
            update={
                "timezone": timezone,
                "created_at": memory.created_at.astimezone(ZoneInfo(timezone)),
                "updated_at": memory.updated_at.astimezone(ZoneInfo(timezone)),
            }
        )

    def create_memory(
        self, user_sub: str, content: str, memory_type: str, tags: list[str]
    ) -> Memory:
        user_sub = self._validate_sub(user_sub)
        data = MemoryCreate(content=content, memory_type=memory_type, tags=tags)
        user = self.session.scalar(select(UserRecord).where(UserRecord.user_sub == user_sub))
        if user is None:
            user = UserRecord(user_sub=user_sub)
            # A concurrent first request may have already created this profile.
            try:
                with self.session.begin_nested():
                    self.session.add(user)
                    self.session.flush()
            except IntegrityError:
                if (
                    self.session.scalar(select(UserRecord).where(UserRecord.user_sub == user_sub))
                    is None
                ):
                    raise
        record = MemoryRecord(
            user_sub=user_sub,
            content=data.content,
            memory_type=data.memory_type,
            tags=list(dict.fromkeys(data.tags)),
        )
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        return self._to_memory(record)

    def get_memory(self, user_sub: str, memory_id: str) -> Memory:
        return self._to_memory(self._get_record(user_sub, memory_id))

    def list_memories(
        self, user_sub: str, skip: int = 0, limit: int = 50, tags: list[str] | None = None
    ) -> list[Memory]:
        user_sub = self._validate_sub(user_sub)
        TypeAdapter(Annotated[int, Field(ge=0, le=2**63 - 1)]).validate_python(skip)
        TypeAdapter(Annotated[int, Field(ge=1, le=100)]).validate_python(limit)
        statement = select(MemoryRecord).where(MemoryRecord.user_sub == user_sub)
        if tags is not None:
            tags = TypeAdapter(Tags).validate_python(tags)
            for tag in dict.fromkeys(tags):
                if self.session.get_bind().dialect.name == "sqlite":
                    tag_values = func.json_each(MemoryRecord.tags).table_valued("value")
                    statement = statement.where(
                        exists(select(1).select_from(tag_values).where(tag_values.c.value == tag))
                    )
                else:
                    raise ValueError("Tag filtering is currently supported only with SQLite")
        records = self.session.scalars(
            statement.order_by(MemoryRecord.created_at.desc(), MemoryRecord.id)
            .offset(skip)
            .limit(limit)
        )
        return [self._to_memory(record) for record in records]

    def update_memory(self, user_sub: str, memory_id: str, **updates: Any) -> Memory:
        data = MemoryUpdate.model_validate(updates)
        record = self._get_record(user_sub, memory_id)
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(record, field, list(dict.fromkeys(value)) if field == "tags" else value)
        record.updated_at = utc_now()
        self.session.commit()
        self.session.refresh(record)
        return self._to_memory(record)

    def delete_memory(self, user_sub: str, memory_id: str) -> bool:
        try:
            record = self._get_record(user_sub, memory_id)
        except MemoryNotFoundError:
            return False
        self.session.delete(record)
        self.session.commit()
        return True
