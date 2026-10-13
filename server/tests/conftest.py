from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from app.config import settings


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


@pytest.fixture
def ddb_table() -> Iterator[Any]:
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
        yield table


@pytest.fixture
def ddb(ddb_table: Any) -> Any:
    return ddb_table


@pytest.fixture
def service(ddb_table: Any) -> Any:
    from app.services.memory_service import MemoryService

    return MemoryService(table=ddb_table)
