"""测试夹具：确定性时钟 + 独立存储 + FastAPI TestClient。"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.models import Plan

T0 = datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def env():
    app = create_app()
    client = TestClient(app)
    app.state.clock.set(T0)
    store = app.state.store

    store.put_plan(Plan(
        code="basic", name="Basic", base_price_cents=1000,
        included_units=100, overage_unit_price_cents=5))
    store.put_plan(Plan(
        code="pro", name="Pro", base_price_cents=3000,
        included_units=500, overage_unit_price_cents=2))
    return {
        "app": app, "client": client, "store": store,
        "clock": app.state.clock, "t0": T0,
    }


def iso(dt):
    return dt.isoformat()
