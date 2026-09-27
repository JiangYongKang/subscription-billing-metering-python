import logging
import pytest
from fastapi.testclient import TestClient

import main
from app import billing
from app.config import RetryConfig
from app.payments import MockGateway
from app.store import STORE, reset_store

logging.basicConfig(level=logging.WARNING)  # 用例默认安静；需要时可设 INFO


@pytest.fixture
def store():
    reset_store(STORE)
    billing.reset_counters()
    return STORE


@pytest.fixture
def config():
    # 快速重试节奏，便于测试：出账当天 +1/+3/+7 天，宽限 14 天
    return RetryConfig(retry_intervals_days=(1, 3, 7), grace_days=14)


@pytest.fixture
def gateway():
    return MockGateway()


@pytest.fixture
def client():
    reset_store(STORE)
    billing.reset_counters()
    return TestClient(main.app)
