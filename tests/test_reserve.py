"""Tests for US-B2B-08: POST /api/v1/reserve and /unreserve."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.base import get_db as real_get_db
from src.models.inventory import ReserveOperation, UnreserveOperation
from src.models.product import Product, ProductStatus
from src.models.sku import SKU
from src.settings import settings

SKU_A = UUID("00000000-0000-4000-8000-000000000701")
SKU_B = UUID("00000000-0000-4000-8000-000000000702")
KEY = UUID("00000000-0000-4000-8000-000000000800")
ORDER = UUID("00000000-0000-4000-8000-000000000900")


def _product(status=ProductStatus.MODERATED) -> MagicMock:
    p = MagicMock(spec=Product)
    p.id = uuid4()
    p.status = status
    p.deleted = False
    return p


def _sku(sku_id: UUID, active: int, reserved: int = 0, *, product=None) -> MagicMock:
    sku = MagicMock(spec=SKU)
    prod = product or _product()
    sku.id = sku_id
    sku.product_id = prod.id
    sku.product = prod
    sku.active_quantity = active
    sku.blocked_quantity = reserved
    sku.stock_quantity = active + reserved
    return sku


class _Session:
    def __init__(
        self,
        skus: dict[UUID, MagicMock],
        reserve_ops: dict | None = None,
        unreserve_ops: dict | None = None,
    ):
        self.skus = skus
        self.reserve_ops = reserve_ops or {}
        self.unreserve_ops = unreserve_ops or {}
        self.added: list = []
        self.committed = False
        self.rolled_back = False

    async def get(self, model, ident, **kwargs):
        if model is ReserveOperation:
            return self.reserve_ops.get(ident)
        if model is UnreserveOperation:
            return self.unreserve_ops.get(ident)
        if model is SKU:
            return self.skus.get(ident)
        if model is Product:
            for sku in self.skus.values():
                if getattr(sku, "product", None) and sku.product.id == ident:
                    return sku.product
            return None
        return None

    async def execute(self, query):
        result = MagicMock()
        result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=list(self.skus.values())))
        )
        return result

    def add(self, obj):
        self.added.append(obj)
        key = getattr(obj, "idempotency_key", None)
        if key is not None and not isinstance(obj, UnreserveOperation):
            self.reserve_ops[key] = obj
        if isinstance(obj, UnreserveOperation):
            self.unreserve_ops[obj.order_id] = obj

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def make_client(session: _Session):
    async def override_get_db():
        yield session

    app.dependency_overrides[real_get_db] = override_get_db
    return TestClient(
        app,
        base_url="http://test",
        headers={"X-Service-Key": settings.b2c_to_b2b_key},
    )


def _reserve_body(*, key=None, order_id=None, items=None):
    return {
        "idempotency_key": str(key or uuid4()),
        "order_id": str(order_id or ORDER),
        "items": items
        or [
            {"sku_id": str(SKU_A), "quantity": 2},
            {"sku_id": str(SKU_B), "quantity": 1},
        ],
    }


@pytest.mark.asyncio
async def test_reserve_all_skus_succeeds():
    a = _sku(SKU_A, active=5, reserved=0)
    b = _sku(SKU_B, active=3, reserved=1)
    session = _Session({SKU_A: a, SKU_B: b})
    client = make_client(session)

    with patch("src.services.inventory.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx
        response = client.post("/api/v1/inventory/reserve", json=_reserve_body(key=KEY))

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "RESERVED"
    assert data["order_id"] == str(ORDER)
    assert "reserved_at" in data
    assert a.active_quantity == 3
    assert a.blocked_quantity == 2
    assert b.active_quantity == 2
    assert b.blocked_quantity == 2
    assert session.committed is True


@pytest.mark.asyncio
async def test_partial_insufficient_stock_returns_409_all_rollback():
    a = _sku(SKU_A, active=5, reserved=0)
    b = _sku(SKU_B, active=1, reserved=0)
    session = _Session({SKU_A: a, SKU_B: b})
    client = make_client(session)

    response = client.post(
        "/api/v1/inventory/reserve",
        json=_reserve_body(
            items=[
                {"sku_id": str(SKU_A), "quantity": 2},
                {"sku_id": str(SKU_B), "quantity": 5},
            ]
        ),
    )

    assert response.status_code == 409, response.text
    data = response.json()
    assert data["code"] == "INSUFFICIENT_STOCK"
    assert "details" in data
    assert "failed_items" in data["details"]
    failed_ids = {str(item["sku_id"]) for item in data["details"]["failed_items"]}
    assert str(SKU_B) in failed_ids
    assert a.active_quantity == 5
    assert b.active_quantity == 1
    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_idempotent_reserve_returns_200_without_double_deduction():
    a = _sku(SKU_A, active=5, reserved=0)
    cached = ReserveOperation(
        idempotency_key=KEY,
        order_id=ORDER,
        result={
            "order_id": str(ORDER),
            "status": "RESERVED",
            "reserved_at": "2026-01-01T00:00:00Z",
        },
    )
    session = _Session({SKU_A: a}, reserve_ops={KEY: cached})
    client = make_client(session)

    response = client.post(
        "/api/v1/inventory/reserve",
        json=_reserve_body(key=KEY, items=[{"sku_id": str(SKU_A), "quantity": 2}]),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "RESERVED"
    assert a.active_quantity == 5
    assert a.blocked_quantity == 0


@pytest.mark.asyncio
async def test_sku_out_of_stock_event_emitted():
    a = _sku(SKU_A, active=2, reserved=0)
    session = _Session({SKU_A: a})
    client = make_client(session)
    captured: dict = {}

    async def mock_post(url, json=None, headers=None, **kwargs):
        captured["json"] = json
        return MagicMock(status_code=200)

    with patch("src.services.inventory.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx
        response = client.post(
            "/api/v1/inventory/reserve",
            json=_reserve_body(items=[{"sku_id": str(SKU_A), "quantity": 2}]),
        )

    assert response.status_code == 200, response.text
    assert a.active_quantity == 0
    assert a.blocked_quantity == 2
    assert captured.get("json", {}).get("event_type") == "SKU_OUT_OF_STOCK"


@pytest.mark.asyncio
async def test_unreserve_restores_quantities():
    a = _sku(SKU_A, active=1, reserved=3)
    session = _Session({SKU_A: a})
    client = make_client(session)

    response = client.post(
        "/api/v1/inventory/unreserve",
        json={"order_id": str(ORDER), "items": [{"sku_id": str(SKU_A), "quantity": 2}]},
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "UNRESERVED"
    assert data["order_id"] == str(ORDER)
    assert "processed_at" in data
    assert a.active_quantity == 3
    assert a.blocked_quantity == 1


@pytest.mark.asyncio
async def test_unreserve_idempotent_by_order_id():
    a = _sku(SKU_A, active=3, reserved=1)
    cached = UnreserveOperation(
        order_id=ORDER,
        result={
            "order_id": str(ORDER),
            "status": "UNRESERVED",
            "processed_at": "2026-01-01T00:00:00Z",
        },
    )
    session = _Session({SKU_A: a}, unreserve_ops={ORDER: cached})
    client = make_client(session)

    response = client.post(
        "/api/v1/inventory/unreserve",
        json={"order_id": str(ORDER), "items": [{"sku_id": str(SKU_A), "quantity": 1}]},
    )

    assert response.status_code == 200, response.text
    assert a.active_quantity == 3
    assert a.blocked_quantity == 1


@pytest.mark.asyncio
async def test_reserve_blocked_product_returns_409():
    blocked = _product(ProductStatus.BLOCKED)
    a = _sku(SKU_A, active=5, reserved=0, product=blocked)
    session = _Session({SKU_A: a})
    client = make_client(session)

    response = client.post(
        "/api/v1/inventory/reserve",
        json=_reserve_body(items=[{"sku_id": str(SKU_A), "quantity": 1}]),
    )

    assert response.status_code == 409, response.text
    data = response.json()
    assert data.get("details") and data["details"].get("failed_items")
    reasons = {item.get("reason") for item in data["details"]["failed_items"]}
    assert "PRODUCT_BLOCKED" in reasons
    assert a.active_quantity == 5
    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_reserve_sums_duplicate_sku_lines():
    a = _sku(SKU_A, active=5, reserved=0)
    session = _Session({SKU_A: a})
    client = make_client(session)

    response = client.post(
        "/api/v1/inventory/reserve",
        json=_reserve_body(
            items=[
                {"sku_id": str(SKU_A), "quantity": 3},
                {"sku_id": str(SKU_A), "quantity": 3},
            ]
        ),
    )

    assert response.status_code == 409, response.text
    assert a.active_quantity == 5
