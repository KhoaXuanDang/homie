from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app.middleware.auth import CurrentUserDependency
from app.models.media import Media, MediaCreate, MediaUpdate, MediaUpload
from app.models.memory import Limit
from app.services.media_service import MediaService

router = APIRouter(prefix="/api/media", tags=["media"])


def get_media_service() -> MediaService:
    return MediaService()


MediaServiceDependency = Annotated[MediaService, Depends(get_media_service)]


@router.post("", response_model=MediaUpload, status_code=201)
def create_media(
    data: MediaCreate,
    user: CurrentUserDependency,
    service: MediaServiceDependency,
    request: Request,
) -> MediaUpload:
    request.app.state.media_rate_limiter.check(user.sub)
    return service.create_media(user.sub, data)


@router.post(
    "/{media_id}/complete",
    response_model=Media,
    responses={409: {"description": "Upload missing or does not match its declared metadata"}},
)
def complete_media(
    media_id: UUID, user: CurrentUserDependency, service: MediaServiceDependency
) -> Media:
    return service.complete_media(user.sub, str(media_id))


@router.get("", response_model=list[Media])
def list_media(
    user: CurrentUserDependency,
    service: MediaServiceDependency,
    limit: Annotated[Limit, Query()] = 50,
) -> list[Media]:
    return service.list_media(user.sub, limit)


@router.patch("/{media_id}", response_model=Media)
def update_media(
    media_id: UUID,
    data: MediaUpdate,
    user: CurrentUserDependency,
    service: MediaServiceDependency,
) -> Media:
    return service.update_media(user.sub, str(media_id), data)


@router.delete("/{media_id}", status_code=204)
def delete_media(
    media_id: UUID, user: CurrentUserDependency, service: MediaServiceDependency
) -> Response:
    service.delete_media(user.sub, str(media_id))
    return Response(status_code=204)
