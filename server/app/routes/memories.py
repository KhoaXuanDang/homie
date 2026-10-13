from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.middleware.auth import get_user_sub
from app.models.memory import Limit, Memory, MemoryCreate, MemoryUpdate, Skip, Tags
from app.services.memory_service import MemoryService

router = APIRouter(prefix="/memories", tags=["memories"])
UserSubDependency = Annotated[str, Depends(get_user_sub)]


def get_memory_service() -> MemoryService:
    return MemoryService()


MemoryServiceDependency = Annotated[MemoryService, Depends(get_memory_service)]


@router.post("", response_model=Memory, status_code=201)
def create_memory(
    data: MemoryCreate, user_sub: UserSubDependency, service: MemoryServiceDependency
) -> Memory:
    return service.create_memory(user_sub, data.content, data.memory_type, data.tags)


@router.get("", response_model=list[Memory])
def list_memories(
    user_sub: UserSubDependency,
    service: MemoryServiceDependency,
    skip: Annotated[Skip, Query()] = 0,
    limit: Annotated[Limit, Query()] = 50,
    tags: Annotated[Tags | None, Query()] = None,
) -> list[Memory]:
    return service.list_memories(user_sub, skip, limit, tags)


@router.get("/{memory_id}", response_model=Memory)
def get_memory(
    memory_id: str, user_sub: UserSubDependency, service: MemoryServiceDependency
) -> Memory:
    return service.get_memory(user_sub, memory_id)


@router.put("/{memory_id}", response_model=Memory)
def update_memory(
    memory_id: str,
    data: MemoryUpdate,
    user_sub: UserSubDependency,
    service: MemoryServiceDependency,
) -> Memory:
    return service.update_memory(user_sub, memory_id, **data.model_dump(exclude_unset=True))


@router.delete("/{memory_id}", status_code=204)
def delete_memory(
    memory_id: str, user_sub: UserSubDependency, service: MemoryServiceDependency
) -> Response:
    if not service.delete_memory(user_sub, memory_id):
        raise HTTPException(404, "Memory not found")
    return Response(status_code=204)
