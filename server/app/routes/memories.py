from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

from app.middleware.auth import CurrentUserDependency
from app.models.memory import Kind, Limit, Memory, MemoryCreate, MemoryUpdate
from app.services.memory_service import MemoryService

router = APIRouter(prefix="/api/memories", tags=["memories"])


def get_memory_service() -> MemoryService:
    return MemoryService()


MemoryServiceDependency = Annotated[MemoryService, Depends(get_memory_service)]


@router.post(
    "",
    response_model=Memory,
    status_code=201,
    responses={
        409: {"description": "Idempotency conflict or request already in progress"},
        410: {"description": "Memory created by this request was deleted"},
    },
)
def create_memory(
    data: MemoryCreate,
    user: CurrentUserDependency,
    service: MemoryServiceDependency,
    request_id: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> Memory:
    return service.create_memory(
        user.sub, data.content, data.kind, data.tags, data.event_id, request_id
    )


@router.get("", response_model=list[Memory])
def list_memories(
    user: CurrentUserDependency,
    service: MemoryServiceDependency,
    limit: Annotated[Limit, Query()] = 50,
    kind: Kind | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[Memory]:
    return service.list_memories(user.sub, kind=kind, query=q, limit=limit)


@router.get("/{memory_id}", response_model=Memory)
def get_memory(
    memory_id: UUID, user: CurrentUserDependency, service: MemoryServiceDependency
) -> Memory:
    return service.get_memory(user.sub, str(memory_id))


@router.patch("/{memory_id}", response_model=Memory)
def update_memory(
    memory_id: UUID,
    data: MemoryUpdate,
    user: CurrentUserDependency,
    service: MemoryServiceDependency,
) -> Memory:
    return service.update_memory(user.sub, str(memory_id), **data.model_dump(exclude_unset=True))


@router.delete("/{memory_id}", status_code=204)
def delete_memory(
    memory_id: UUID, user: CurrentUserDependency, service: MemoryServiceDependency
) -> Response:
    if not service.delete_memory(user.sub, str(memory_id)):
        raise HTTPException(404, "Memory not found")
    return Response(status_code=204)
