import logging
import time
import uuid
from datetime import UTC, datetime
from functools import reduce
from threading import local
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import TypeAdapter, ValidationError

from app.config import settings
from app.models.memory import Limit, Memory, MemoryCreate, MemoryUpdate, Skip, Tags
from app.models.user import Profile, UserSub

logger = logging.getLogger(__name__)

_user_sub_adapter = TypeAdapter(UserSub)
_skip_adapter = TypeAdapter(Skip)
_limit_adapter = TypeAdapter(Limit)
_tags_adapter = TypeAdapter(Tags)
# boto3 resources and the default session are not thread-safe; sync routes run in a threadpool.
_thread = local()


class MemoryNotFoundError(Exception):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def get_dynamodb_resource() -> Any:
    return boto3.session.Session().resource(
        "dynamodb", region_name=settings.aws_region, endpoint_url=settings.dynamodb_endpoint_url
    )


def get_table() -> Any:
    if getattr(_thread, "table", None) is None:
        _thread.table = get_dynamodb_resource().Table(settings.dynamodb_table)
    return _thread.table


def ensure_table(attempts: int = 10, delay: float = 1.0) -> None:
    ddb = get_dynamodb_resource()
    for attempt in range(attempts):
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
            return
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceInUseException":
                return
            raise
        except EndpointConnectionError:
            # DynamoDB Local may still be starting; fail startup loudly if it never comes up.
            if attempt == attempts - 1:
                raise
            logger.warning("Waiting for DynamoDB at %s", settings.dynamodb_endpoint_url)
            time.sleep(delay)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class MemoryService:
    def __init__(self, table: Any = None) -> None:
        self.table = table if table is not None else get_table()

    def _validate_sub(self, user_sub: str) -> str:
        return _user_sub_adapter.validate_python(user_sub)

    def _timezone(self, user_sub: str) -> tuple[str, ZoneInfo]:
        item = self.table.get_item(Key={"pk": f"USER#{user_sub}", "sk": "PROFILE"}).get("Item")
        try:
            tz_name = Profile(timezone=item["timezone"]).timezone if item else "UTC"
        except KeyError, ValidationError:
            logger.warning("Invalid stored profile timezone; using UTC")
            tz_name = "UTC"
        return tz_name, ZoneInfo(tz_name)

    @staticmethod
    def _to_memory(record: dict[str, Any], timezone: str, zone: ZoneInfo) -> Memory:
        return Memory(
            id=record["id"],
            user_sub=record["user_sub"],
            content=record["content"],
            memory_type=record["kind"],
            tags=list(record.get("tags", [])),
            created_at=_parse_utc(record["created_at"]).astimezone(zone),
            updated_at=_parse_utc(record["updated_at"]).astimezone(zone),
            timezone=timezone,
        )

    def create_memory(
        self, user_sub: str, content: str, memory_type: str, tags: list[str]
    ) -> Memory:
        user_sub = self._validate_sub(user_sub)
        data = MemoryCreate(content=content, memory_type=memory_type, tags=tags)
        # A missing PROFILE means defaults (UTC); profiles are created by the profile endpoint.
        memory_id = str(uuid.uuid7())
        now_iso = utc_now().isoformat()
        item = {
            "pk": f"USER#{user_sub}",
            "sk": f"MEM#{memory_id}",
            "id": memory_id,
            "user_sub": user_sub,
            "content": data.content,
            "kind": data.memory_type,
            "tags": list(dict.fromkeys(data.tags)),
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
        unique_tags = list(dict.fromkeys(_tags_adapter.validate_python(tags or [])))

        # UUIDv7 sort keys are time-ordered, so a descending query is newest-first.
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"USER#{user_sub}")
            & Key("sk").begins_with("MEM#"),
            "ScanIndexForward": False,
        }
        if unique_tags:
            query_kwargs["FilterExpression"] = reduce(
                lambda a, b: a & b, (Attr("tags").contains(tag) for tag in unique_tags)
            )
        # dev-note: offset paging reads skip+limit items; switch to a cursor if histories grow big.
        wanted = skip + limit
        items: list[dict[str, Any]] = []
        while len(items) < wanted:
            # Limit counts items before the filter, so only cap pages when nothing is filtered.
            if not unique_tags:
                query_kwargs["Limit"] = wanted - len(items)
            response = self.table.query(**query_kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        timezone, zone = self._timezone(user_sub)
        return [self._to_memory(item, timezone, zone) for item in items[skip:wanted]]

    def update_memory(self, user_sub: str, memory_id: str, **updates: Any) -> Memory:
        user_sub = self._validate_sub(user_sub)
        data = MemoryUpdate.model_validate(updates)
        if not memory_id:
            raise MemoryNotFoundError("Memory not found")

        changes = data.model_dump(exclude_unset=True)
        sets: dict[str, Any] = {}
        if "content" in changes:
            sets["content"] = changes["content"]
        if "memory_type" in changes:
            sets["kind"] = changes["memory_type"]
        if "tags" in changes:
            sets["tags"] = list(dict.fromkeys(changes["tags"]))
        sets["updated_at"] = utc_now().isoformat()

        try:
            response = self.table.update_item(
                Key={"pk": f"USER#{user_sub}", "sk": f"MEM#{memory_id}"},
                UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in sets),
                ConditionExpression="attribute_exists(pk) AND attribute_exists(sk)",
                ExpressionAttributeNames={f"#{k}": k for k in sets},
                ExpressionAttributeValues={f":{k}": v for k, v in sets.items()},
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise MemoryNotFoundError("Memory not found") from exc
            raise
        return self._to_memory(response["Attributes"], *self._timezone(user_sub))

    def delete_memory(self, user_sub: str, memory_id: str) -> bool:
        user_sub = self._validate_sub(user_sub)
        if not memory_id:
            return False
        try:
            self.table.delete_item(
                Key={"pk": f"USER#{user_sub}", "sk": f"MEM#{memory_id}"},
                ConditionExpression="attribute_exists(pk) AND attribute_exists(sk)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True
