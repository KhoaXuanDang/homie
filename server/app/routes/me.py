from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.middleware.auth import CurrentUserDependency
from app.models.user import Profile, ProfileUpdate
from app.services.profile_service import ProfileService

router = APIRouter(prefix="/api/me", tags=["me"])


def get_profile_service() -> ProfileService:
    return ProfileService()


ProfileServiceDependency = Annotated[ProfileService, Depends(get_profile_service)]


@router.get("/profile", response_model=Profile)
def get_profile(user: CurrentUserDependency, service: ProfileServiceDependency) -> Profile:
    return service.get_profile(user.sub)


@router.put("/profile", response_model=Profile)
def put_profile(
    data: ProfileUpdate, user: CurrentUserDependency, service: ProfileServiceDependency
) -> Profile:
    return service.put_profile(user.sub, data)


@router.delete("", status_code=204)
def delete_me(user: CurrentUserDependency, service: ProfileServiceDependency) -> Response:
    service.delete_all_user_data(user.sub)
    return Response(status_code=204)
