import logging
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from pydantic import TypeAdapter, ValidationError

from app.config import settings
from app.models.user import Profile, ProfileUpdate, UserSub
from app.services.memory_service import get_table

logger = logging.getLogger(__name__)

_user_sub_adapter = TypeAdapter(UserSub)


def get_s3_client() -> Any:
    return boto3.session.Session().client("s3", region_name=settings.aws_region)


class ProfileService:
    def __init__(self, table: Any = None, s3: Any = None) -> None:
        self.table = table if table is not None else get_table()
        self._s3 = s3

    @staticmethod
    def _validate_sub(user_sub: str) -> str:
        user_sub = _user_sub_adapter.validate_python(user_sub)
        # Keeps the S3 prefix users/{sub}/ from reaching another user's objects.
        if "/" in user_sub:
            raise ValueError("Invalid user")
        return user_sub

    @staticmethod
    def _to_profile(item: dict[str, Any] | None) -> Profile:
        if not item:
            return Profile()
        try:
            return Profile.model_validate(
                {key: item[key] for key in ("timezone", "home", "travel_mode") if key in item}
            )
        except ValidationError:
            logger.warning("Invalid stored profile; using defaults")
            return Profile()

    def get_profile(self, user_sub: str) -> Profile:
        user_sub = self._validate_sub(user_sub)
        item = self.table.get_item(
            Key={"pk": f"USER#{user_sub}", "sk": "PROFILE"}, ConsistentRead=True
        ).get("Item")
        return self._to_profile(item)

    def put_profile(self, user_sub: str, data: ProfileUpdate) -> Profile:
        user_sub = self._validate_sub(user_sub)
        current = self.get_profile(user_sub)
        changes = data.model_dump(exclude_unset=True)
        item: dict[str, Any] = {
            "pk": f"USER#{user_sub}",
            "sk": "PROFILE",
            "timezone": changes.get("timezone", current.timezone),
            "travel_mode": changes.get("travel_mode", current.travel_mode),
        }
        if "home" in changes:
            # dev-note: stores the address only; Plan 2 geocodes it server-side into lat/lng.
            home = changes["home"]
        else:
            home = current.home and current.home.model_dump(exclude_none=True)
        if home:
            item["home"] = {
                key: Decimal(str(value)) if isinstance(value, float) else value
                for key, value in home.items()
            }
        self.table.put_item(Item=item)
        return self._to_profile(item)

    def delete_all_user_data(self, user_sub: str) -> None:
        """Delete every USER#{sub} item and the user's S3 prefix. Safe to repeat."""
        user_sub = self._validate_sub(user_sub)
        # dev-note: no Google integration yet; Plan 2 must revoke the GOOGLE refresh token
        # here, before its item is deleted below.
        # S3 first: if it fails, the records remain and a retry repeats the whole delete.
        if settings.media_bucket:
            self._delete_media(user_sub)
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"USER#{user_sub}"),
            "ProjectionExpression": "pk, sk",
            "ConsistentRead": True,
        }
        with self.table.batch_writer() as batch:
            while True:
                response = self.table.query(**query_kwargs)
                for item in response.get("Items", []):
                    batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                if "LastEvaluatedKey" not in response:
                    break
                query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _delete_media(self, user_sub: str) -> None:
        s3 = self._s3 if self._s3 is not None else get_s3_client()
        pages = s3.get_paginator("list_objects_v2").paginate(
            Bucket=settings.media_bucket, Prefix=f"users/{user_sub}/"
        )
        for page in pages:
            # list_objects_v2 pages hold at most 1000 keys, the delete_objects limit.
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                s3.delete_objects(
                    Bucket=settings.media_bucket, Delete={"Objects": keys, "Quiet": True}
                )
