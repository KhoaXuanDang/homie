import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from threading import local
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import TypeAdapter

from app.config import settings
from app.models.memory import Kind, Limit, Memory, MemoryCreate, MemoryUpdate
from app.models.user import UserSub
from app.services.storage import (
    memory_media_ids,
    parse_utc,
    profile_timezone,
    storage_errors,
    utc_timestamp,
)

logger = logging.getLogger(__name__)

_user_sub_adapter = TypeAdapter(UserSub)
_limit_adapter = TypeAdapter(Limit)
_kind_adapter = TypeAdapter(Kind)
_MAX_ITEMS = 2000
_Ranked = TypeVar("_Ranked")
_STOPWORDS = frozenset(
    "a an and are at did do does for i in is it its me my of on or put the this to was "
    "were what when where which who why you your left keep kept".split()
)
# boto3 resources and the default session are not thread-safe; sync routes run in a threadpool.
_thread = local()


class MemoryNotFoundError(Exception):
    pass


class IdempotencyConflictError(Exception):
    pass


class IdempotencyInProgressError(Exception):
    pass


class IdempotencyRecordDeletedError(Exception):
    pass


class AccountDeletingError(Exception):
    pass


# Marker item present while an account deletion is removing the user's records.
DELETING_SK = "DELETING"


def write_unless_deleting(table: Any, user_sub: str, items: list[dict[str, Any]]) -> None:
    check = {
        "ConditionCheck": {
            "TableName": table.name,
            "Key": {"pk": f"USER#{user_sub}", "sk": DELETING_SK},
            "ConditionExpression": "attribute_not_exists(pk)",
        }
    }
    try:
        table.meta.client.transact_write_items(TransactItems=[*items, check])
    except ClientError as exc:
        reasons = exc.response.get("CancellationReasons", [])
        if len(reasons) == len(items) + 1 and reasons[-1].get("Code") == "ConditionalCheckFailed":
            raise AccountDeletingError("Account deletion in progress") from exc
        raise


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


class MemoryService:
    @storage_errors
    def __init__(self, table: Any = None) -> None:
        self.table = table if table is not None else get_table()

    def _validate_sub(self, user_sub: str) -> str:
        return _user_sub_adapter.validate_python(user_sub)

    def _timezone(self, user_sub: str) -> tuple[str, ZoneInfo]:
        return profile_timezone(self.table, user_sub)

    @staticmethod
    def _to_memory(record: dict[str, Any], timezone: str, zone: ZoneInfo) -> Memory:
        return Memory(
            id=record["id"],
            user_sub=record["user_sub"],
            content=record["content"],
            kind=record["kind"],
            tags=list(record.get("tags", [])),
            event_id=record.get("event_id"),
            media_ids=list(record.get("media_ids", [])),
            created_at=parse_utc(record["created_at"]).astimezone(zone),
            updated_at=parse_utc(record["updated_at"]).astimezone(zone),
            timezone=timezone,
        )

    @storage_errors
    def create_memory(
        self,
        user_sub: str,
        content: str,
        kind: Kind,
        tags: list[str],
        event_id: str | None = None,
        request_id: uuid.UUID | str | None = None,
    ) -> Memory:
        user_sub = self._validate_sub(user_sub)
        data = MemoryCreate(content=content, kind=kind, tags=tags, event_id=event_id)
        # A missing PROFILE means defaults (UTC); profiles are created by the profile endpoint.
        memory_id = str(uuid.uuid7())
        now_iso = utc_timestamp(utc_now())
        item = {
            "pk": f"USER#{user_sub}",
            "sk": f"MEM#{memory_id}",
            "id": memory_id,
            "user_sub": user_sub,
            "content": data.content,
            "kind": data.kind,
            "tags": data.tags,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        if data.event_id is not None:
            item["event_id"] = data.event_id

        if request_id is not None:
            request_uuid = uuid.UUID(str(request_id))
            request_sk = f"REQUEST#create-memory#{request_uuid}"
            request_item = {
                "pk": f"USER#{user_sub}",
                "sk": request_sk,
                "memory_id": memory_id,
                "payload_hash": self._payload_hash(data),
                "created_at": now_iso,
            }
            existing = self.table.get_item(
                Key={"pk": request_item["pk"], "sk": request_sk}, ConsistentRead=True
            ).get("Item")
            if existing:
                return self._resolve_idempotent_replay(
                    user_sub, existing, request_item["payload_hash"]
                )

            try:
                self.table.meta.client.transact_write_items(
                    TransactItems=[
                        {
                            "Put": {
                                "TableName": self.table.name,
                                "Item": item,
                                "ConditionExpression": "attribute_not_exists(pk)",
                            }
                        },
                        {
                            "Put": {
                                "TableName": self.table.name,
                                "Item": request_item,
                                "ConditionExpression": "attribute_not_exists(pk)",
                            }
                        },
                    ]
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                    raise
                reasons = exc.response.get("CancellationReasons", [])
                if not any(
                    reason.get("Code") == "ConditionalCheckFailed" for reason in reasons
                ) or any(
                    reason.get("Code") not in {"None", "ConditionalCheckFailed"}
                    for reason in reasons
                ):
                    raise
                # Only treat a failed transaction as a replay if its request record now exists.
                existing = self.table.get_item(
                    Key={"pk": request_item["pk"], "sk": request_sk}, ConsistentRead=True
                ).get("Item")
                if existing:
                    return self._resolve_idempotent_replay(
                        user_sub, existing, request_item["payload_hash"]
                    )
                raise IdempotencyInProgressError("Request in progress, try again") from exc
        else:
            self.table.put_item(Item=item)
        return self._to_memory(item, *self._timezone(user_sub))

    @staticmethod
    def _payload_hash(data: MemoryCreate) -> str:
        canonical = json.dumps(data.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _resolve_idempotent_replay(
        self, user_sub: str, request_item: dict[str, Any], payload_hash: str
    ) -> Memory:
        if request_item["payload_hash"] != payload_hash:
            raise IdempotencyConflictError("Idempotency key was already used with another payload")
        response = self.table.get_item(
            Key={"pk": f"USER#{user_sub}", "sk": f"MEM#{request_item['memory_id']}"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if item is None:
            raise IdempotencyRecordDeletedError("The memory created by this request was deleted")
        # P0 intentionally replays the memory's latest state, even if it changed after creation.
        return self._to_memories(user_sub, [item])[0]

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {
            word.removesuffix("s") if len(word) > 3 else word
            for word in re.findall(r"[a-z0-9]+", text.lower())
            if word not in _STOPWORDS
        }

    @classmethod
    def _rank_records(cls, records: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        return cls._rank_items(
            records,
            query,
            lambda record: f"{record['content']} {' '.join(record.get('tags', []))}",
        )

    @classmethod
    def _rank_items(
        cls, items: list[_Ranked], query: str, text: Callable[[_Ranked], str]
    ) -> list[_Ranked]:
        query_tokens = cls._tokens(query)
        if not query_tokens:
            return []
        scored = [(len(query_tokens & cls._tokens(text(item))), item) for item in items]
        return [
            item
            for score, item in sorted(scored, key=lambda entry: entry[0], reverse=True)
            if score
        ]

    @storage_errors
    def get_memory(self, user_sub: str, memory_id: str) -> Memory:
        user_sub = self._validate_sub(user_sub)
        if not memory_id:
            raise MemoryNotFoundError("Memory not found")
        response = self.table.get_item(
            Key={"pk": f"USER#{user_sub}", "sk": f"MEM#{memory_id}"}, ConsistentRead=True
        )
        item = response.get("Item")
        if not item:
            raise MemoryNotFoundError("Memory not found")
        return self._to_memories(user_sub, [item])[0]

    def _read_memory_records(
        self, user_sub: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        # UUIDv7 sort keys are time-ordered, so a descending query is newest-first.
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"USER#{user_sub}")
            & Key("sk").begins_with("MEM#"),
            "ScanIndexForward": False,
        }
        if limit is not None:
            items: list[dict[str, Any]] = []
            while len(items) < limit:
                query_kwargs["Limit"] = limit - len(items)
                response = self.table.query(**query_kwargs)
                items.extend(response.get("Items", []))
                if "LastEvaluatedKey" not in response:
                    break
                query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            return items[:limit]

        items: list[dict[str, Any]] = []
        while len(items) < _MAX_ITEMS:
            query_kwargs["Limit"] = _MAX_ITEMS - len(items)
            response = self.table.query(**query_kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response or len(items) >= _MAX_ITEMS:
                break
            query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        return items[:_MAX_ITEMS]

    def _to_memories(
        self, user_sub: str, records: list[dict[str, Any]], *, include_media_ids: bool = True
    ) -> list[Memory]:
        if not records:
            return []
        timezone, zone = self._timezone(user_sub)
        links = (
            memory_media_ids(self.table, user_sub, {record["id"] for record in records})
            if include_media_ids
            else {}
        )
        return [
            self._to_memory(
                {**record, "media_ids": links.get(record["id"], [])},
                timezone,
                zone,
            )
            for record in records
        ]

    @storage_errors
    def list_memories(
        self,
        user_sub: str,
        kind: Kind | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        user_sub = self._validate_sub(user_sub)
        _limit_adapter.validate_python(limit)
        if kind is not None:
            _kind_adapter.validate_python(kind)
        has_query = bool(query and query.strip())
        records = self._read_memory_records(
            user_sub,
            limit=limit if kind is None and not has_query else None,
        )
        if kind is not None:
            records = [record for record in records if record["kind"] == kind]
        if has_query:
            records = self._rank_records(records, query.strip())
        return self._to_memories(user_sub, records[:limit], include_media_ids=False)

    @storage_errors
    def recall(
        self, user_sub: str, query: str, kind: Kind | None = None, limit: int = 10
    ) -> list[Memory]:
        _limit_adapter.validate_python(limit)
        user_sub = self._validate_sub(user_sub)
        if kind is not None:
            _kind_adapter.validate_python(kind)

        result_limit = min(limit, 10)
        search_query = query.strip()
        records = self._read_memory_records(
            user_sub, limit=result_limit if kind is None and not search_query else None
        )
        if kind is not None:
            records = [record for record in records if record["kind"] == kind]
        if search_query:
            ranked = self._rank_records(records, search_query)
            if ranked:
                records = ranked
        return self._to_memories(user_sub, records[:result_limit], include_media_ids=False)

    @storage_errors
    def update_memory(self, user_sub: str, memory_id: str, **updates: Any) -> Memory:
        user_sub = self._validate_sub(user_sub)
        data = MemoryUpdate.model_validate(updates)
        if not memory_id:
            raise MemoryNotFoundError("Memory not found")

        changes = data.model_dump(exclude_unset=True)
        sets: dict[str, Any] = {}
        if "content" in changes:
            sets["content"] = changes["content"]
        if "kind" in changes:
            sets["kind"] = changes["kind"]
        if "tags" in changes:
            sets["tags"] = changes["tags"]
        removes: list[str] = []
        if "event_id" in changes:
            if changes["event_id"] is None:
                removes.append("event_id")
            else:
                sets["event_id"] = changes["event_id"]
        sets["updated_at"] = utc_timestamp(utc_now())
        expression = "SET " + ", ".join(f"#{k} = :{k}" for k in sets)
        if removes:
            expression += " REMOVE " + ", ".join(f"#{key}" for key in removes)

        try:
            response = self.table.update_item(
                Key={"pk": f"USER#{user_sub}", "sk": f"MEM#{memory_id}"},
                UpdateExpression=expression,
                ConditionExpression="attribute_exists(pk) AND attribute_exists(sk)",
                ExpressionAttributeNames={f"#{k}": k for k in [*sets, *removes]},
                ExpressionAttributeValues={f":{k}": v for k, v in sets.items()},
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise MemoryNotFoundError("Memory not found") from exc
            raise
        return self._to_memories(user_sub, [response["Attributes"]])[0]

    @storage_errors
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
