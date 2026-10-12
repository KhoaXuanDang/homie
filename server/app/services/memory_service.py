import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from pydantic import TypeAdapter

from app.config import settings
from app.models.memory import Limit, Memory, MemoryCreate, MemoryUpdate, Skip, Tags
from app.models.user import Profile, UserSub

logger = logging.getLogger(__name__)

_user_sub_adapter = TypeAdapter(UserSub)
_skip_adapter = TypeAdapter(Skip)
_limit_adapter = TypeAdapter(Limit)
_tags_adapter = TypeAdapter(Tags)


class MemoryNotFoundError(Exception):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    if hasattr(uuid, "uuid7"):
        return str(uuid.uuid7())
    return str(uuid.uuid4())


def get_dynamodb_resource() -> Any:
    kwargs: dict[str, Any] = {"region_name": settings.aws_region}
    if settings.dynamodb_endpoint_url:
        kwargs["endpoint_url"] = settings.dynamodb_endpoint_url
    return boto3.resource("dynamodb", **kwargs)


def get_table() -> Any:
    return get_dynamodb_resource().Table(settings.dynamodb_table)


def ensure_table() -> None:
    ddb = get_dynamodb_resource()
    try:
        ddb.create_table(
            TableName=settings.dynamodb_table,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in (
            "ResourceInUseException",
            "TableAlreadyExistsException",
        ):
            logger.warning("Could not auto-create DynamoDB table: %s", exc)
    except Exception as exc:
        logger.warning("Could not auto-create DynamoDB table: %s", exc)


class MemoryService:
    def __init__(self, table: Any = None) -> None:
        self.table = table if table is not None else get_table()

    def _validate_sub(self, user_sub: str) -> str:
        return _user_sub_adapter.validate_python(user_sub)

    def _timezone(self, user_sub: str) -> tuple[str, ZoneInfo]:
        try:
            response = self.table.get_item(Key={"pk": f"USER#{user_sub}", "sk": "PROFILE"})
            item = response.get("Item")
            if item and "timezone" in item:
                tz_name = Profile(timezone=item["timezone"]).timezone
                return tz_name, ZoneInfo(tz_name)
        except Exception:
            pass
        return "UTC", ZoneInfo("UTC")

    @staticmethod
    def _to_memory(record: dict[str, Any], timezone: str, zone: ZoneInfo) -> Memory:
        created_raw = record["created_at"]
        if isinstance(created_raw, datetime):
            created_dt = created_raw
        else:
            created_dt = datetime.fromisoformat(str(created_raw))
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=UTC)

        updated_raw = record["updated_at"]
        if isinstance(updated_raw, datetime):
            updated_dt = updated_raw
        else:
            updated_dt = datetime.fromisoformat(str(updated_raw))
        if updated_dt.tzinfo is None:
            updated_dt = updated_dt.replace(tzinfo=UTC)

        return Memory(
            id=record["id"],
            user_sub=record["user_sub"],
            content=record["content"],
            memory_type=record.get("memory_type") or record.get("kind", ""),
            tags=list(record.get("tags", [])),
            created_at=created_dt.astimezone(zone),
            updated_at=updated_dt.astimezone(zone),
            timezone=timezone,
        )

    def create_memory(
        self, user_sub: str, content: str, memory_type: str, tags: list[str]
    ) -> Memory:
        user_sub = self._validate_sub(user_sub)
        data = MemoryCreate(content=content, memory_type=memory_type, tags=tags)

        now_iso = utc_now().isoformat()
        try:
            self.table.put_item(
                Item={
                    "pk": f"USER#{user_sub}",
                    "sk": "PROFILE",
                    "timezone": "UTC",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

        memory_id = new_id()
        deduped_tags = list(dict.fromkeys(data.tags))
        item = {
            "pk": f"USER#{user_sub}",
            "sk": f"MEM#{memory_id}",
            "id": memory_id,
            "user_sub": user_sub,
            "content": data.content,
            "memory_type": data.memory_type,
            "kind": data.memory_type,
            "tags": deduped_tags,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        self.table.put_item(Item=item)
        return self._to_memory(item, *self._timezone(user_sub))

    def get_memory(self, user_sub: str, memory_id: str) -> Memory:
        user_sub = self._validate_sub(user_sub)
        if not memory_id:
            raise MemoryNotFoundError("Memory not found")
        response = self.table.get_item(Key={"pk": f"USER#{user_sub}", "sk": f"MEM#{memory_id}"})
        item = response.get("Item")
        if not item:
            raise MemoryNotFoundError("Memory not found")
        return self._to_memory(item, *self._timezone(user_sub))

    def list_memories(
        self,
        user_sub: str,
        skip: int = 0,
        limit: int = 50,
        tags: list[str] | None = None,
    ) -> list[Memory]:
        user_sub = self._validate_sub(user_sub)
        _skip_adapter.validate_python(skip)
        _limit_adapter.validate_python(limit)
        validated_tags: list[str] | None = None
        if tags is not None:
            validated_tags = _tags_adapter.validate_python(tags)

        key_cond = Key("pk").eq(f"USER#{user_sub}") & Key("sk").begins_with("MEM#")
        query_kwargs: dict[str, Any] = {"KeyConditionExpression": key_cond}
        items: list[dict[str, Any]] = []
        while True:
            response = self.table.query(**query_kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response or len(items) >= 2000:
                break
            query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        if validated_tags is not None:
            unique_tags = list(dict.fromkeys(validated_tags))
            if unique_tags:
                req_tags_set = set(unique_tags)
                items = [i for i in items if req_tags_set.issubset(set(i.get("tags", [])))]

        items.sort(key=lambda x: (x.get("created_at", ""), x.get("id", "")), reverse=True)
        sliced = items[skip : skip + limit]
        timezone, zone = self._timezone(user_sub)
        return [self._to_memory(item, timezone, zone) for item in sliced]

    def update_memory(self, user_sub: str, memory_id: str, **updates: Any) -> Memory:
        user_sub = self._validate_sub(user_sub)
        data = MemoryUpdate.model_validate(updates)
        if not memory_id:
            raise MemoryNotFoundError("Memory not found")

        changes = data.model_dump(exclude_unset=True)
        sets: dict[str, Any] = {}
        for k, v in changes.items():
            if k == "tags":
                sets["tags"] = list(dict.fromkeys(v))
            elif k == "memory_type":
                sets["memory_type"] = v
                sets["kind"] = v
            else:
                sets[k] = v
        sets["updated_at"] = utc_now().isoformat()

        update_expr = "SET " + ", ".join(f"#{k} = :{k}" for k in sets)
        expr_attr_names = {f"#{k}": k for k in sets}
        expr_attr_values = {f":{k}": v for k, v in sets.items()}

        try:
            response = self.table.update_item(
                Key={"pk": f"USER#{user_sub}", "sk": f"MEM#{memory_id}"},
                UpdateExpression=update_expr,
                ConditionExpression="attribute_exists(pk) AND attribute_exists(sk)",
                ExpressionAttributeNames=expr_attr_names,
                ExpressionAttributeValues=expr_attr_values,
                ReturnValues="ALL_NEW",
            )
            item = response["Attributes"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise MemoryNotFoundError("Memory not found") from exc
            raise
        return self._to_memory(item, *self._timezone(user_sub))

    def delete_memory(self, user_sub: str, memory_id: str) -> bool:
        if not memory_id:
            return False
        try:
            user_sub = self._validate_sub(user_sub)
        except Exception:
            return False

        try:
            response = self.table.delete_item(
                Key={"pk": f"USER#{user_sub}", "sk": f"MEM#{memory_id}"},
                ConditionExpression="attribute_exists(pk) AND attribute_exists(sk)",
                ReturnValues="ALL_OLD",
            )
            return "Attributes" in response
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
