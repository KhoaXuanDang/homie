from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid7

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import TypeAdapter

from app.config import settings
from app.models.media import Media, MediaCreate, MediaUpdate, MediaUpload
from app.models.memory import Limit
from app.models.user import UserSub
from app.services.memory_service import (
    MemoryNotFoundError,
    MemoryService,
    _parse_utc,
    get_table,
    utc_now,
)

UPLOAD_EXPIRY = 300
VIEW_EXPIRY = 900
_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
}


class MediaNotFoundError(Exception):
    pass


class UploadNotReadyError(Exception):
    pass


class MediaStorageError(Exception):
    pass


class MediaService:
    def __init__(self, table: Any = None, s3: Any = None) -> None:
        self.table = table if table is not None else get_table()
        self.s3 = (
            s3
            if s3 is not None
            else boto3.session.Session().client(
                "s3",
                region_name=settings.aws_region,
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=5,
                    read_timeout=10,
                    retries={"max_attempts": 2},
                ),
            )
        )

    @staticmethod
    def _key(user_sub: str, media_id: str) -> dict[str, str]:
        sub = TypeAdapter(UserSub).validate_python(user_sub)
        return {"pk": f"USER#{sub}", "sk": f"MEDIA#{media_id}"}

    def _storage(self, operation: str, **kwargs: Any) -> Any:
        if not settings.media_bucket:
            raise MediaStorageError
        try:
            return getattr(self.s3, operation)(**kwargs)
        except (BotoCoreError, ClientError) as exc:
            raise MediaStorageError from exc

    def _presign(self, operation: str, item: dict[str, Any], expiry: int) -> str:
        params = {"Bucket": settings.media_bucket, "Key": item["s3_key"]}
        if operation == "put_object":
            params.update(ContentType=item["content_type"], ContentLength=int(item["size"]))
        else:
            params["ResponseCacheControl"] = "no-store"
        return self._storage(
            "generate_presigned_url", ClientMethod=operation, Params=params, ExpiresIn=expiry
        )

    def _to_media(self, item: dict[str, Any], user_sub: str) -> Media:
        _, zone = MemoryService(table=self.table)._timezone(user_sub)
        return Media(
            **{key: value for key, value in item.items() if key != "created_at"},
            created_at=_parse_utc(item["created_at"]).astimezone(zone),
            url=self._presign("get_object", item, VIEW_EXPIRY)
            if item["status"] == "uploaded"
            else "",
        )

    def _get(self, user_sub: str, media_id: str) -> dict[str, Any]:
        item = self.table.get_item(Key=self._key(user_sub, media_id), ConsistentRead=True).get(
            "Item"
        )
        if not item or item["status"] == "deleting":
            raise MediaNotFoundError
        return item

    def create_media(self, user_sub: str, data: MediaCreate) -> MediaUpload:
        media_id = str(uuid7())
        key = self._key(user_sub, media_id)
        item = {
            **key,
            "id": media_id,
            "s3_key": f"users/{user_sub}/media/{media_id}.{_EXTENSIONS[data.content_type]}",
            **data.model_dump(),
            "tags": [],
            "training_consent": False,
            "status": "pending",
            "created_at": utc_now().isoformat(),
        }
        upload_url = self._presign("put_object", item, UPLOAD_EXPIRY)
        self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        return MediaUpload(
            media=self._to_media(item, user_sub),
            upload_url=upload_url,
            upload_headers={
                "Content-Type": data.content_type,
                "Content-Length": str(data.size),
            },
            expires_in=UPLOAD_EXPIRY,
        )

    def complete_media(self, user_sub: str, media_id: str) -> Media:
        item = self._get(user_sub, media_id)
        if item["status"] == "uploaded":
            return self._to_media(item, user_sub)
        try:
            head = self._storage("head_object", Bucket=settings.media_bucket, Key=item["s3_key"])
        except MediaStorageError as exc:
            cause = exc.__cause__
            if isinstance(cause, ClientError) and cause.response["Error"]["Code"] in {
                "404",
                "NoSuchKey",
                "NotFound",
            }:
                raise UploadNotReadyError from exc
            raise
        if head["ContentLength"] != item["size"] or head["ContentType"] != item["content_type"]:
            raise UploadNotReadyError
        try:
            item = self.table.update_item(
                Key=self._key(user_sub, media_id),
                UpdateExpression="SET #status = :uploaded",
                ConditionExpression="#status IN (:pending, :uploaded)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":pending": "pending", ":uploaded": "uploaded"},
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise MediaNotFoundError from exc
            raise
        return self._to_media(item, user_sub)

    def _items(self, user_sub: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
        params = {
            "KeyConditionExpression": Key("pk").eq(self._key(user_sub, "")["pk"])
            & Key("sk").begins_with("MEDIA#"),
            "ScanIndexForward": False,
            "ConsistentRead": True,
            "Limit": 200,
            **kwargs,
        }
        while True:
            page = self.table.query(**params)
            yield from page.get("Items", [])
            if not page.get("LastEvaluatedKey"):
                break
            params["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def list_media(self, user_sub: str, limit: int = 50) -> list[Media]:
        limit = TypeAdapter(Limit).validate_python(limit)
        self.cleanup_pending(user_sub)
        result = []
        for item in self._items(user_sub, FilterExpression=Attr("status").eq("uploaded")):
            result.append(self._to_media(item, user_sub))
            if len(result) == limit:
                break
        return result

    def update_media(self, user_sub: str, media_id: str, data: MediaUpdate) -> Media:
        self._get(user_sub, media_id)
        if data.memory_id is not None:
            if not self.table.get_item(
                Key={"pk": self._key(user_sub, "")["pk"], "sk": f"MEM#{data.memory_id}"},
                ConsistentRead=True,
            ).get("Item"):
                raise MemoryNotFoundError
        changes = data.model_dump(mode="json", exclude_unset=True)
        names = {f"#f{i}": field for i, field in enumerate(changes)}
        values = {f":v{i}": value for i, value in enumerate(changes.values())}
        try:
            item = self.table.update_item(
                Key=self._key(user_sub, media_id),
                UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(changes))),
                ConditionExpression="#status IN (:pending, :uploaded)",
                ExpressionAttributeNames={**names, "#status": "status"},
                ExpressionAttributeValues={
                    **values,
                    ":pending": "pending",
                    ":uploaded": "uploaded",
                },
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise MediaNotFoundError from exc
            raise
        return self._to_media(item, user_sub)

    def _claim_delete(self, item: dict[str, Any], *, cutoff: str | None = None) -> bool:
        condition = "attribute_exists(pk)"
        values: dict[str, Any] = {":deleting": "deleting"}
        if cutoff is not None:
            condition = "#status = :pending AND created_at < :cutoff"
            values.update({":pending": "pending", ":cutoff": cutoff})
        try:
            self.table.update_item(
                Key={"pk": item["pk"], "sk": item["sk"]},
                UpdateExpression="SET #status = :deleting",
                ConditionExpression=condition,
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def _finish_delete(self, item: dict[str, Any]) -> None:
        # Keep the deletion marker until S3 succeeds, so a later list/delete can retry.
        self._storage("delete_object", Bucket=settings.media_bucket, Key=item["s3_key"])
        try:
            params = {
                "Key": {"pk": item["pk"], "sk": item["sk"]},
                "ConditionExpression": "#status = :deleting",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {":deleting": "deleting"},
            }
            if _parse_utc(item["created_at"]) >= utc_now() - timedelta(hours=1):
                # A signed PUT can still arrive after deletion; retain tracking through
                # the abandonment window and remove any late object on subsequent lists.
                params["ExpressionAttributeValues"][":done"] = True
                self.table.update_item(
                    **params,
                    UpdateExpression="SET deletion_complete = :done",
                )
            else:
                self.table.delete_item(**params)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def delete_media(self, user_sub: str, media_id: str) -> None:
        item = self.table.get_item(Key=self._key(user_sub, media_id), ConsistentRead=True).get(
            "Item"
        )
        if not item or item.get("deletion_complete") or not self._claim_delete(item):
            raise MediaNotFoundError
        self._finish_delete(item)

    def cleanup_pending(self, user_sub: str) -> int:
        cutoff = (utc_now() - timedelta(hours=1)).isoformat()
        deleted = 0
        for item in self._items(
            user_sub,
            FilterExpression=Attr("status").eq("deleting")
            | (Attr("status").eq("pending") & Attr("created_at").lt(cutoff)),
        ):
            if item["status"] == "deleting" or self._claim_delete(item, cutoff=cutoff):
                self._finish_delete(item)
                deleted += 1
        return deleted
