"""Tests for US-B2B-09: POST /api/v1/events/moderation."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.base import get_db as real_get_db
from src.models.moderation_events import ProcessedModerationEvent
from src.models.product import Product, ProductStatus
from src.settings import settings

PRODUCT_UUID = UUID("00000000-0000-4000-8000-000000000601")
REASON_UUID = UUID("00000000-0000-4000-8000-000000000777")
SELLER_UUID = UUID("00000000-0000-4000-8000-000000000042")


def _product(status=ProductStatus.ON_MODERATION):
    p = MagicMock(spec=Product)
    p.id = PRODUCT_UUID
    p.seller_id = SELLER_UUID
    p.status = status
    p.deleted = False
    p.blocking_reason_id = None
    p.blocking_comment = None
    p.field_reports = None
    return p


class _Session:
    def __init__(self, product=None, processed=None, has_sku=True):
        self.product = product
        self.processed = processed or {}
        self.added = []
        self.committed = False
        self.has_sku = has_sku

    async def get(self, model, ident, **kwargs):
        if model is ProcessedModerationEvent:
            return self.processed.get(ident)
        if model is Product:
            if self.product and ident == self.product.id:
                return self.product
            return None
        return None

    async def execute(self, query):
        result = MagicMock()
        skus = [MagicMock()] if self.has_sku else []
        result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=skus))
        )
        return result

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, ProcessedModerationEvent):
            self.processed[obj.idempotency_key] = obj

    async def commit(self):
        self.committed = True

    async def flush(self):
        return None

    async def rollback(self):
        return None


def make_client(session: _Session):
    async def override_get_db():
        yield session

    app.dependency_overrides[real_get_db] = override_get_db
    return TestClient(
        app,
        base_url="http://test",
        headers={"X-Service-Key": settings.b2b_to_mod_key},
    )


def _event_body(**overrides):
    body = {
        "idempotency_key": str(uuid4()),
        "product_id": str(PRODUCT_UUID),
        "event_type": "MODERATED",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_moderated_event_clears_blocking_data():
    product = _product(status=ProductStatus.ON_MODERATION)
    product.blocking_reason_id = REASON_UUID
    product.blocking_comment = "old"
    product.field_reports = [{"field_name": "title", "comment": "x"}]
    session = _Session(product)
    client = make_client(session)

    with patch("src.services.moderation.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=202))
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post("/api/v1/events/moderation", json=_event_body())

    assert response.status_code == 204, response.text
    assert product.status == ProductStatus.MODERATED
    assert product.blocking_reason_id is None
    assert product.blocking_comment is None
    assert product.field_reports is None


@pytest.mark.asyncio
async def test_blocked_soft_saves_field_reports():
    product = _product(status=ProductStatus.ON_MODERATION)
    session = _Session(product)
    client = make_client(session)
    captured = {}

    async def mock_post(url, json=None, headers=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return MagicMock(status_code=202)

    with patch("src.services.moderation.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post(
            "/api/v1/events/moderation",
            json=_event_body(
                event_type="BLOCKED",
                hard_block=False,
                blocking_reason_id=str(REASON_UUID),
                moderator_comment="Несоответствие описания и фотографий",
                field_reports=[
                    {
                        "field_name": "description",
                        "sku_id": None,
                        "comment": "Текст описания скопирован с другого товара",
                    }
                ],
            ),
        )

    assert response.status_code == 204, response.text
    assert product.status == ProductStatus.BLOCKED
    assert product.field_reports is not None
    assert product.field_reports[0]["field_name"] == "description"
    assert product.blocking_reason_id == REASON_UUID
    # B2C OpenAPI B2BEvent
    assert captured["url"].endswith("/api/v1/b2b/events")
    assert captured["json"]["event_type"] == "PRODUCT_BLOCKED"
    assert captured["json"]["payload"]["product_id"] == str(PRODUCT_UUID)
    assert "idempotency_key" in captured["json"]
    assert "occurred_at" in captured["json"]
    assert captured["headers"].get("X-Service-Key") == settings.b2b_to_b2c_key


@pytest.mark.asyncio
async def test_blocked_hard_sets_terminal_status():
    product = _product(status=ProductStatus.ON_MODERATION)
    session = _Session(product)
    client = make_client(session)
    captured = {}

    async def mock_post(url, json=None, headers=None, **kwargs):
        captured["json"] = json
        return MagicMock(status_code=202)

    with patch("src.services.moderation.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post(
            "/api/v1/events/moderation",
            json=_event_body(
                event_type="BLOCKED",
                hard_block=True,
                blocking_reason_id=str(REASON_UUID),
            ),
        )

    assert response.status_code == 204, response.text
    assert product.status == ProductStatus.HARD_BLOCKED
    assert captured["json"]["event_type"] == "PRODUCT_HARD_BLOCKED"


@pytest.mark.asyncio
async def test_hard_blocked_product_rejects_seller_edits():
    """HARD_BLOCKED rejects published mutation methods (PATCH/DELETE product)."""
    from src.models.base import get_db as real_get_db

    product = _product(status=ProductStatus.HARD_BLOCKED)
    session = _Session(product)

    async def override_get_db():
        yield session

    app.dependency_overrides[real_get_db] = override_get_db
    client = TestClient(
        app,
        base_url="http://test",
        headers={"X-Seller-Id": str(SELLER_UUID)},
    )

    r_patch = client.patch(
        f"/api/v1/products/{PRODUCT_UUID}",
        json={"title": "New title"},
    )
    assert r_patch.status_code == 403, r_patch.text
    assert r_patch.json()["code"] in ("PRODUCT_HARD_BLOCKED", "FORBIDDEN")

    r_del = client.delete(f"/api/v1/products/{PRODUCT_UUID}")
    assert r_del.status_code == 403, r_del.text
    assert r_del.json()["code"] in ("PRODUCT_HARD_BLOCKED", "FORBIDDEN")

    r_img = client.post(
        f"/api/v1/products/{PRODUCT_UUID}/images",
        json={"url": "https://example.com/x.jpg"},
    )
    assert r_img.status_code == 403, r_img.text


@pytest.mark.asyncio
async def test_duplicate_event_same_idempotency_key_no_side_effects():
    product = _product(status=ProductStatus.MODERATED)
    key = uuid4()
    processed = ProcessedModerationEvent(
        idempotency_key=key,
        product_id=PRODUCT_UUID,
        event_status="MODERATED",
        result=None,
    )
    session = _Session(product, processed={key: processed})
    client = make_client(session)

    with patch("src.services.moderation.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=202))
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post(
            "/api/v1/events/moderation",
            json=_event_body(idempotency_key=str(key), event_type="MODERATED"),
        )

    assert response.status_code == 204, response.text
    # no second cascade
    assert mock_client.post.await_count == 0


@pytest.mark.asyncio
async def test_blocked_without_reason_returns_400():
    product = _product(status=ProductStatus.ON_MODERATION)
    session = _Session(product)
    client = make_client(session)

    response = client.post(
        "/api/v1/events/moderation",
        json=_event_body(event_type="BLOCKED", hard_block=False),
    )
    assert response.status_code == 400, response.text
    data = response.json()
    assert data["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_moderation_missing_required_fields_returns_400():
    """OpenAPI required fields missing → 400 VALIDATION_ERROR (not silent defaults)."""
    product = _product(status=ProductStatus.ON_MODERATION)
    session = _Session(product)
    client = make_client(session)

    response = client.post(
        "/api/v1/moderation/events",
        json={
            "idempotency_key": str(uuid4()),
            "product_id": str(PRODUCT_UUID),
            "event_type": "MODERATED",
            # occurred_at intentionally omitted
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_moderation_invalid_event_type_returns_400():
    product = _product(status=ProductStatus.ON_MODERATION)
    session = _Session(product)
    client = make_client(session)

    response = client.post(
        "/api/v1/moderation/events",
        json={
            "idempotency_key": str(uuid4()),
            "product_id": str(PRODUCT_UUID),
            "event_type": "APPROVED",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_moderation_missing_event_type_returns_400():
    """Body without required event_type must be rejected (no status→event_type mapping)."""
    product = _product(status=ProductStatus.ON_MODERATION)
    session = _Session(product)
    client = make_client(session)

    response = client.post(
        "/api/v1/moderation/events",
        json={
            "idempotency_key": str(uuid4()),
            "product_id": str(PRODUCT_UUID),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            # event_type omitted on purpose
            "status": "MODERATED",  # foreign key must NOT fill event_type
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_moderation_status_alias_alone_rejected():
    """Only status without event_type is invalid under OpenAPI."""
    product = _product(status=ProductStatus.ON_MODERATION)
    session = _Session(product)
    client = make_client(session)

    response = client.post(
        "/api/v1/events/moderation",
        json={
            "idempotency_key": str(uuid4()),
            "product_id": str(PRODUCT_UUID),
            "status": "MODERATED",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
