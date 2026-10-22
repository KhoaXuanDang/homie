from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid7
from zoneinfo import ZoneInfo

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
    get_table,
    utc_now,
    write_unless_deleting,
)
from app.services.storage import (
    StorageError,
    existing_keys,
    parse_utc,
    profile_timezone,
    storage_errors,
    utc_timestamp,
)

UPLOAD_EXPIRY = 300
VIEW_EXPIRY = 900
_LIST_SCAN_LIMIT = 200
_user_sub_adapter = TypeAdapter(UserSub)
_limit_adapter = TypeAdapter(Limit)
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


MediaStorageError = StorageError


class MediaService:
    @storage_errors
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
        sub = _user_sub_adapter.validate_python(user_sub)
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

    def _to_media(self, item: dict[str, Any], zone: ZoneInfo) -> Media:
        return Media(
            **{key: value for key, value in item.items() if key != "created_at"},
            created_at=parse_utc(item["created_at"]).astimezone(zone),
            url=self._presign("get_object", item, VIEW_EXPIRY)
            if item["status"] == "uploaded"
            else "",
        )

    def _to_medias(self, items: list[dict[str, Any]], user_sub: str) -> list[Media]:
        user_sub = _user_sub_adapter.validate_python(user_sub)
        _, zone = profile_timezone(self.table, user_sub)
        memory_keys = [
            {"pk": self._key(user_sub, "")["pk"], "sk": f"MEM#{item['memory_id']}"}
            for item in items
            if item.get("memory_id")
        ]
        existing = existing_keys(self.table, memory_keys)
        return [
            self._to_media(
                {
                    **item,
                    "memory_id": item.get("memory_id")
                    if f"MEM#{item.get('memory_id')}" in existing
                    else None,
                },
                zone,
            )
            for item in items
        ]

    def _get(self, user_sub: str, media_id: str) -> dict[str, Any]:
        item = self.table.get_item(Key=self._key(user_sub, media_id), ConsistentRead=True).get(
            "Item"
        )
        if not item or item["status"] == "deleting":
            raise MediaNotFoundError
        return item

    @storage_errors
    def create_media(self, user_sub: str, data: MediaCreate) -> MediaUpload:
        user_sub = _user_sub_adapter.validate_python(user_sub)
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
            "created_at": utc_timestamp(utc_now()),
        }
        upload_url = self._presign("put_object", item, UPLOAD_EXPIRY)
        write_unless_deleting(
            self.table,
            user_sub,
            [
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": item,
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                }
            ],
        )
        try:
            self.maintain(user_sub, limit=100)
        except StorageError:
            pass
        return MediaUpload(
            media=self._to_medias([item], user_sub)[0],
            upload_url=upload_url,
            upload_headers={
                "Content-Type": data.content_type,
                "Content-Length": str(data.size),
            },
            expires_in=UPLOAD_EXPIRY,
        )

    @storage_errors
    def complete_media(self, user_sub: str, media_id: str) -> Media:
        item = self._get(user_sub, media_id)
        if item["status"] == "uploaded":
            return self._to_medias([item], user_sub)[0]
        try:
            head = self._storage("head_object", Bucket=settings.media_bucket, Key=item["s3_key"])
        except MediaStorageError as exc:
            cause = exc.__cause__
            if isinstance(cause, ClientError) and cause.response["Error"]["Code"] in {
                "403",
                "AccessDenied",
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
        return self._to_medias([item], user_sub)[0]

    @storage_errors
    def list_media(self, user_sub: str, limit: int = 50) -> list[Media]:
        limit = _limit_adapter.validate_python(limit)
        page = self.table.query(
            KeyConditionExpression=Key("pk").eq(self._key(user_sub, "")["pk"])
            & Key("sk").begins_with("MEDIA#"),
            ScanIndexForward=False,
            ConsistentRead=True,
            Limit=_LIST_SCAN_LIMIT,
            FilterExpression=Attr("status").eq("uploaded"),
        )
        return self._to_medias(page.get("Items", [])[:limit], user_sub)

    @storage_errors
    def update_media(self, user_sub: str, media_id: str, data: MediaUpdate) -> Media:
        self._get(user_sub, media_id)
        changes = data.model_dump(mode="json", exclude_unset=True)
        names = {f"#f{i}": field for i, field in enumerate(changes)}
        values = {f":v{i}": value for i, value in enumerate(changes.values())}
        try:
            update = dict(
                Key=self._key(user_sub, media_id),
                UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(changes))),
                ConditionExpression="#status IN (:pending, :uploaded)",
                ExpressionAttributeNames={**names, "#status": "status"},
                ExpressionAttributeValues={
                    **values,
                    ":pending": "pending",
                    ":uploaded": "uploaded",
                },
            )
            items = []
            if data.memory_id is not None:
                items.append(
                    {
                        "ConditionCheck": {
                            "TableName": self.table.name,
                            "Key": {
                                "pk": self._key(user_sub, "")["pk"],
                                "sk": f"MEM#{data.memory_id}",
                            },
                            "ConditionExpression": "attribute_exists(pk)",
                        }
                    }
                )
            items.append({"Update": {"TableName": self.table.name, **update}})
            write_unless_deleting(self.table, user_sub, items)
            item = self._get(user_sub, media_id)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                reasons = exc.response.get("CancellationReasons", [])
                if any(
                    reason.get("Code") not in {"None", "ConditionalCheckFailed"}
                    for reason in reasons
                ):
                    raise
                if (
                    data.memory_id is not None
                    and reasons
                    and reasons[0].get("Code") == "ConditionalCheckFailed"
                ):
                    raise MemoryNotFoundError from exc
                update_index = 1 if data.memory_id is not None else 0
                if (
                    len(reasons) > update_index
                    and reasons[update_index].get("Code") == "ConditionalCheckFailed"
                ):
                    raise MediaNotFoundError from exc
            raise
        return self._to_medias([item], user_sub)[0]

    def _claim_delete(self, item: dict[str, Any], *, cutoff: datetime | None = None) -> bool:
        condition = "#status IN (:pending, :uploaded)"
        values: dict[str, Any] = {
            ":deleting": "deleting",
            ":pending": "pending",
            ":uploaded": "uploaded",
        }
        if cutoff is not None:
            if parse_utc(item["created_at"]) >= cutoff:
                return False
            condition = "#status = :pending AND created_at = :snapshot"
            values.pop(":uploaded")
            values[":snapshot"] = item["created_at"]
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
        # Failed removals remain retryable; fresh completed markers need no S3 work.
        if item.get("deletion_complete") and parse_utc(item["created_at"]) >= utc_now() - timedelta(
            hours=1
        ):
            return
        self._storage("delete_object", Bucket=settings.media_bucket, Key=item["s3_key"])
        try:
            params = {
                "Key": {"pk": item["pk"], "sk": item["sk"]},
                "ConditionExpression": "#status = :deleting AND created_at = :snapshot",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {
                    ":deleting": "deleting",
                    ":snapshot": item["created_at"],
                },
            }
            if parse_utc(item["created_at"]) >= utc_now() - timedelta(hours=1):
                # A signed PUT can still arrive after deletion; retain tracking through
                # the abandonment window and remove late objects after retention.
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

    @storage_errors
    def delete_media(self, user_sub: str, media_id: str) -> None:
        item = self._get(user_sub, media_id)
        if not self._claim_delete(item):
            raise MediaNotFoundError
        self._finish_delete(item)

    def _cleanup_page(self, items: list[dict[str, Any]]) -> int:
        cutoff = utc_now() - timedelta(hours=1)
        deleted = 0
        for item in items:
            if item.get("deletion_complete") and parse_utc(item["created_at"]) >= cutoff:
                continue
            if item["status"] == "deleting" or (
                item["status"] == "pending" and self._claim_delete(item, cutoff=cutoff)
            ):
                self._finish_delete(item)
                deleted += 1
        return deleted

    @storage_errors
    def cleanup_pending(self, user_sub: str) -> int:
        """Compatibility helper: process at most 200 records, never an unbounded sweep."""
        page = self._media_page(user_sub, 200)
        return self._cleanup_page(page.get("Items", []))

    def _media_page(
        self, user_sub: str, limit: int, cursor: dict[str, str] | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(self._key(user_sub, "")["pk"])
            & Key("sk").begins_with("MEDIA#"),
            "Limit": limit,
            "ConsistentRead": True,
        }
        if cursor:
            if (
                set(cursor) != {"pk", "sk"}
                or cursor["pk"] != self._key(user_sub, "")["pk"]
                or not cursor["sk"].startswith("MEDIA#")
            ):
                raise ValueError("Invalid media maintenance checkpoint")
            params["ExclusiveStartKey"] = cursor
        return self.table.query(**params)

    def _orphan_page(
        self, user_sub: str, limit: int, cursor: str | None = None
    ) -> tuple[int, str | None]:
        prefix = f"users/{user_sub}/media/"
        params = {"Bucket": settings.media_bucket, "Prefix": prefix, "MaxKeys": limit}
        if cursor:
            params["ContinuationToken"] = cursor
        cutoff = utc_now() - timedelta(hours=1)
        page = self._storage("list_objects_v2", **params)
        candidates = []
        for obj in page.get("Contents", []):
            if not obj["Key"].startswith(prefix) or obj["LastModified"] >= cutoff:
                continue
            filename = obj["Key"][len(prefix) :]
            try:
                media_id = str(UUID(filename.rsplit(".", 1)[0]))
            except ValueError:
                continue
            candidates.append((obj["Key"], self._key(user_sub, media_id)))
        tracked = existing_keys(self.table, [key for _, key in candidates])
        deleted = 0
        for object_key, key in candidates:
            if key["sk"] not in tracked:
                self._storage("delete_object", Bucket=settings.media_bucket, Key=object_key)
                deleted += 1
        return deleted, page.get("NextContinuationToken") if page.get("IsTruncated") else None

    @storage_errors
    def maintain(self, user_sub: str, limit: int = 100) -> dict[str, Any]:
        """Process one bounded page, saving progress only after the whole page succeeds."""
        user_sub = _user_sub_adapter.validate_python(user_sub)
        limit = _limit_adapter.validate_python(limit)
        key = {"pk": self._key(user_sub, "")["pk"], "sk": "MAINTENANCE#media"}
        state = self.table.get_item(Key=key, ConsistentRead=True).get("Item", {})
        phase = state.get("phase", "media")
        cursor = state.get("cursor")
        if phase == "media":
            if cursor is not None and not isinstance(cursor, dict):
                raise ValueError("Invalid media maintenance checkpoint")
            page = self._media_page(user_sub, limit, cursor)
            deleted = self._cleanup_page(page.get("Items", []))
            next_cursor = page.get("LastEvaluatedKey")
            next_phase = "media" if next_cursor else "orphans"
        elif phase == "orphans":
            if cursor is not None and not isinstance(cursor, str):
                raise ValueError("Invalid orphan maintenance checkpoint")
            deleted, next_cursor = self._orphan_page(user_sub, limit, cursor)
            next_phase = "orphans" if next_cursor else "media"
        else:
            raise ValueError("Invalid media maintenance phase")
        checkpoint = {**key, "phase": next_phase, "version": str(uuid7())}
        if next_cursor:
            checkpoint["cursor"] = next_cursor
        params: dict[str, Any] = {
            "Item": checkpoint,
            "ConditionExpression": "#version = :version" if state else "attribute_not_exists(pk)",
        }
        if state:
            params.update(
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues={":version": state["version"]},
            )
        self.table.put_item(**params)
        return {"phase": phase, "deleted": deleted, "next_phase": next_phase}
