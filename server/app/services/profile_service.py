import logging
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from pydantic import TypeAdapter, ValidationError

from app.config import settings
from app.models.user import Profile, ProfileUpdate, UserSub
from app.services.memory_service import DELETING_SK, get_table, write_unless_deleting

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
        changes = data.model_dump(exclude_unset=True)
        sets = {key: changes[key] for key in ("timezone", "travel_mode") if key in changes}
        removes = []
        if "home_address" in changes:
            if changes["home_address"]:
                # dev-note: stores the address only; Plan 2 geocodes it server-side into lat/lng.
                sets["home"] = {"address": changes["home_address"]}
            else:
                removes.append("home")
        expression = " ".join(
            part
            for part in (
                sets and "SET " + ", ".join(f"#{key} = :{key}" for key in sets),
                removes and "REMOVE " + ", ".join(f"#{key}" for key in removes),
            )
            if part
        )
        # Field-level update: concurrent edits to different fields both survive.
        update: dict[str, Any] = {
            "TableName": self.table.name,
            "Key": {"pk": f"USER#{user_sub}", "sk": "PROFILE"},
            "UpdateExpression": expression,
            "ExpressionAttributeNames": {f"#{key}": key for key in [*sets, *removes]},
        }
        if sets:
            update["ExpressionAttributeValues"] = {f":{key}": value for key, value in sets.items()}
        write_unless_deleting(self.table, user_sub, [{"Update": update}])
        return self.get_profile(user_sub)

    def delete_all_user_data(self, user_sub: str) -> None:
        """Delete every USER#{sub} item and the user's S3 prefix. Safe to repeat."""
        user_sub = self._validate_sub(user_sub)
        # dev-note: no Google integration yet; Plan 2 must revoke the GOOGLE refresh token
        # here, before its item is deleted below.
        # The marker blocks new writes until every record is gone (memory_service
        # write_unless_deleting). If anything below fails it stays, so writes keep failing
        # until a retried delete finishes.
        marker = {"pk": f"USER#{user_sub}", "sk": DELETING_SK}
        self.table.put_item(Item=marker)
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
                    if item["sk"] != DELETING_SK:
                        batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                if "LastEvaluatedKey" not in response:
                    break
                query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        self.table.delete_item(Key=marker)

    def _delete_media(self, user_sub: str) -> None:
        s3 = self._s3 if self._s3 is not None else get_s3_client()
        pages = s3.get_paginator("list_objects_v2").paginate(
            Bucket=settings.media_bucket, Prefix=f"users/{user_sub}/"
        )
        for page in pages:
            # list_objects_v2 pages hold at most 1000 keys, the delete_objects limit.
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                response = s3.delete_objects(
                    Bucket=settings.media_bucket, Delete={"Objects": keys, "Quiet": True}
                )
                # Per-object failures come back with HTTP 200; stop before deleting records.
                if response.get("Errors"):
                    raise RuntimeError(f"S3 failed to delete {len(response['Errors'])} objects")
