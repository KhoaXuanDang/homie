import logging
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any
from zoneinfo import ZoneInfo

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from app.models.user import Profile

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Retryable storage failure; never expose the SDK's message to clients."""


def storage_errors[**P, R](operation: Callable[P, R]) -> Callable[P, R]:
    @wraps(operation)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return operation(*args, **kwargs)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError from exc

    return wrapped


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


@storage_errors
def profile_timezone(table: Any, user_sub: str) -> tuple[str, ZoneInfo]:
    item = table.get_item(Key={"pk": f"USER#{user_sub}", "sk": "PROFILE"}, ConsistentRead=True).get(
        "Item"
    )
    try:
        name = Profile(timezone=item["timezone"]).timezone if item else "UTC"
    except KeyError, ValidationError:
        logger.warning("Invalid stored profile timezone; using UTC")
        name = "UTC"
    return name, ZoneInfo(name)


@storage_errors
def existing_keys(table: Any, keys: list[dict[str, str]]) -> set[str]:
    """Consistent, bounded-retry batch reads, without interpreting throttling as absence."""
    found: set[str] = set()
    unique = list({(key["pk"], key["sk"]): key for key in keys}.values())
    for offset in range(0, len(unique), 100):
        request = {table.name: {"Keys": unique[offset : offset + 100], "ConsistentRead": True}}
        for _ in range(3):
            result = table.meta.client.batch_get_item(RequestItems=request)
            found.update(item["sk"] for item in result.get("Responses", {}).get(table.name, []))
            request = result.get("UnprocessedKeys", {})
            if not request:
                break
        else:
            raise StorageError
    return found


@storage_errors
def memory_media_ids(table: Any, user_sub: str, memory_ids: set[str]) -> dict[str, list[str]]:
    """Derive reverse links in one partition traversal, never one query per memory."""
    links: dict[str, list[str]] = {memory_id: [] for memory_id in memory_ids}
    params: dict[str, Any] = {
        "KeyConditionExpression": Key("pk").eq(f"USER#{user_sub}")
        & Key("sk").begins_with("MEDIA#"),
        "ConsistentRead": True,
        "Limit": 200,
    }
    while True:
        page = table.query(**params)
        for item in page.get("Items", []):
            if item.get("status") in {"pending", "uploaded"} and item.get("memory_id") in links:
                links[item["memory_id"]].append(item["id"])
        if not page.get("LastEvaluatedKey"):
            return links
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]
