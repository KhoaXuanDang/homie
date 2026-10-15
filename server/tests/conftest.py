from collections.abc import Iterator
from threading import Lock, local
from typing import Any
from uuid import uuid4

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from moto import mock_aws

from app.config import settings
from app.services import memory_service


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(settings, "aws_region", "us-east-1")
    monkeypatch.setattr(settings, "dynamodb_table", "homie-test")
    monkeypatch.setattr(settings, "dynamodb_endpoint_url", None)
    monkeypatch.setattr(memory_service, "_thread", local())


@pytest.fixture
def ddb_table(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.create_table(
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
        # Moto 5.2.4 currently fails parsing DynamoDB TransactWriteItems. Keep
        # transaction semantics in this fixture while the app uses the real API.
        transaction_lock = Lock()
        deserializer = TypeDeserializer()

        def transact_write_items(
            *, TransactItems: list[dict[str, Any]], **_: Any
        ) -> dict[str, Any]:
            with transaction_lock:
                writes = [operation["Put"] for operation in TransactItems]
                decoded = [
                    {key: deserializer.deserialize(value) for key, value in write["Item"].items()}
                    for write in writes
                ]
                if any(
                    table.get_item(Key={"pk": item["pk"], "sk": item["sk"]}).get("Item")
                    for item in decoded
                ):
                    raise ClientError(
                        {"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems"
                    )
                for item in decoded:
                    table.put_item(Item=item)
            return {}

        monkeypatch.setattr(table.meta.client, "transact_write_items", transact_write_items)
        yield table


@pytest.fixture
def ddb_local_table() -> Iterator[Any]:
    endpoint_url = "http://127.0.0.1:8001"
    client = boto3.client(
        "dynamodb",
        region_name=settings.aws_region,
        endpoint_url=endpoint_url,
        config=Config(connect_timeout=1, read_timeout=1, retries={"max_attempts": 0}),
    )
    try:
        client.list_tables()
    except BotoCoreError as exc:
        pytest.skip(f"DynamoDB Local is not running at {endpoint_url}: {exc}")

    table_name = f"homie-integration-{uuid4().hex}"
    try:
        client.create_table(
            TableName=table_name,
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
        table = boto3.resource(
            "dynamodb", region_name=settings.aws_region, endpoint_url=endpoint_url
        ).Table(table_name)
        table.wait_until_exists()
        yield table
    finally:
        try:
            client.delete_table(TableName=table_name)
        except ClientError:
            pass


@pytest.fixture
def ddb(ddb_table: Any) -> Any:
    return ddb_table


@pytest.fixture
def service(ddb_table: Any) -> Any:
    from app.services.memory_service import MemoryService

    return MemoryService(table=ddb_table)
