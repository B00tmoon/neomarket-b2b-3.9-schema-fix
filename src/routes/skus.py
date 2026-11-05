"""SKU routes — US-B2B-02: POST /api/v1/skus (neomarket-protocols + B2B-2)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.base import get_db
from src.models.product import Product, ProductStatus
from src.models.sku import SKU, SKUCharacteristic, SKUImage
from src.schemas.errors import ErrorCode
from src.schemas.product import SKUCreate, SKUResponse, SKUUpdate
from src.schemas.inventory import ReserveRequest, ReserveResponse, UnreserveRequest, UnreserveResponse
from src.services import inventory as inventory_service
from src.settings import settings

logger = logging.getLogger(__name__)
router = APIRouter()


def _parse_seller_id(request: Request) -> UUID | None:
    seller_id = getattr(request.state, "user", None)
    if seller_id is None:
        seller_id = request.headers.get("X-Seller-Id")
    if seller_id is None:
        return None
    try:
        return UUID(str(seller_id))
    except (ValueError, TypeError, AttributeError):
        return None


def _product_snapshot(product: Product) -> dict:
    """Minimal product snapshot for moderation payload.json_after / json_before."""
    return {
        "id": str(product.id),
        "seller_id": str(product.seller_id),
        "title": product.title,
        "description": product.description or "",
        "status": (
            product.status.value
            if hasattr(product.status, "value")
            else str(product.status)
        ),
        "category_id": str(product.category_id) if product.category_id else None,
        "slug": getattr(product, "slug", None) or "",
        "deleted": bool(getattr(product, "deleted", False)),
    }


async def _emit_moderation_event(
    *,
    event_type: str,
    product: Product,
    json_before: dict | None = None,
) -> None:
    """
    POST to Moderation per neomarket-protocols moderation/openapi.yaml:

      POST {moderation_url}/api/v1/b2b/events
      X-Service-Key: {b2b_to_mod_key}

      {
        "event_type": "PRODUCT_CREATED" | "PRODUCT_EDITED" | "PRODUCT_DELETED",
        "idempotency_key": "<uuid>",
        "occurred_at": "<ISO-8601>",
        "payload": { product_id, seller_id, category_id?, json_after, json_before? }
      }
    """
    url = f"{settings.moderation_service_url.rstrip('/')}/api/v1/b2b/events"
    json_after = _product_snapshot(product)
    payload: dict = {
        "product_id": str(product.id),
        "seller_id": str(product.seller_id),
        "json_after": json_after,
    }
    if product.category_id is not None:
        payload["category_id"] = str(product.category_id)
    if json_before is not None:
        payload["json_before"] = json_before

    body = {
        "event_type": event_type,
        "idempotency_key": str(uuid.uuid4()),
        "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z",
        "payload": payload,
    }
    headers = {"X-Service-Key": settings.b2b_to_mod_key}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=body, headers=headers)
            logger.info(
                "moderation event %s for product %s → %s",
                event_type,
                product.id,
                resp.status_code,
            )
    except Exception as exc:  # noqa: BLE001 — fire-and-forget
        logger.warning(
            "failed to emit moderation event %s for product %s: %s",
            event_type,
            product.id,
            exc,
        )


@router.post("/skus", response_model=SKUResponse, status_code=201)
async def create_sku(
    body: SKUCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SKUResponse:
    """
    POST /api/v1/skus — create a product variant (US-B2B-02).

    Side effects:
    - First SKU on CREATED product → ON_MODERATION + PRODUCT_CREATED
      (serialized via SELECT FOR UPDATE so concurrent first-SKU requests
       emit exactly one PRODUCT_CREATED)
    - SKU on MODERATED/BLOCKED → ON_MODERATION + PRODUCT_EDITED
    - HARD_BLOCKED → 403
    - Already ON_MODERATION → no status change, no event
    """
    seller_id = _parse_seller_id(request)
    if seller_id is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": ErrorCode.UNAUTHORIZED,
                "message": "JWT token required",
            },
        )

    # Lock product row for the whole check+insert so concurrent "first SKU"
    # requests cannot both observe zero SKUs and both emit PRODUCT_CREATED.
    locked = await db.execute(
        select(Product).where(Product.id == body.product_id).with_for_update()
    )
    product = locked.scalar_one_or_none()
    if product is None or product.deleted:
        raise HTTPException(
            status_code=404,
            detail={
                "code": ErrorCode.NOT_FOUND,
                "message": "Product not found",
            },
        )

    # IDOR: seller may only add SKUs to own products (checked BEFORE insert)
    if product.seller_id != seller_id:
        raise HTTPException(
            status_code=404,
            detail={
                "code": ErrorCode.NOT_FOUND,
                "message": "Product not found",
            },
        )

    if product.status == ProductStatus.HARD_BLOCKED:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN",
                "message": "Cannot add SKU to hard-blocked product",
            },
        )

    count_result = await db.execute(
        select(func.count()).select_from(SKU).where(SKU.product_id == body.product_id)
    )
    existing_count = int(count_result.scalar_one() or 0)
    is_first_sku = existing_count == 0
    previous_status = product.status
    json_before = _product_snapshot(product)

    now = datetime.now(timezone.utc)
    sku_id = uuid.uuid4()
    primary_image = body.images[0].url if body.images else None

    db_sku = SKU(
        id=sku_id,
        product_id=body.product_id,
        sku_code=f"SKU-{sku_id.hex[:12]}",
        name=body.name,
        price=body.price,
        cost_price=body.cost_price,
        discount=body.discount,
        article=body.article,
        image=primary_image or "",
        stock_quantity=0,
        active_quantity=0,
        blocked_quantity=0,
        active=True,
        created_at=now,
        updated_at=None,
    )
    db.add(db_sku)
    await db.flush()

    for img in body.images:
        db.add(
            SKUImage(
                id=uuid.uuid4(),
                sku_id=db_sku.id,
                url=img.url,
                ordering=img.ordering,
            )
        )

    for char in body.characteristics:
        db.add(
            SKUCharacteristic(
                id=uuid.uuid4(),
                sku_id=db_sku.id,
                name=char.name,
                value=char.value,
            )
        )

    # Status transition under the same product row lock.
    emit_event: str | None = None
    if is_first_sku and previous_status == ProductStatus.CREATED:
        product.status = ProductStatus.ON_MODERATION
        emit_event = "PRODUCT_CREATED"
    elif previous_status in (ProductStatus.MODERATED, ProductStatus.BLOCKED):
        product.status = ProductStatus.ON_MODERATION
        emit_event = "PRODUCT_EDITED"

    await db.commit()

    result = await db.execute(
        select(SKU)
        .where(SKU.id == db_sku.id)
        .options(
            selectinload(SKU.characteristics),
            selectinload(SKU.images),
        )
    )
    db_sku = result.scalar_one()

    if emit_event:
        await _emit_moderation_event(
            event_type=emit_event,
            product=product,
            json_before=json_before if emit_event == "PRODUCT_EDITED" else None,
        )

    return SKUResponse.from_orm_sku(db_sku)



async def _load_sku_product_or_404(sku_id: UUID, db: AsyncSession) -> tuple[SKU, Product]:
    result = await db.execute(
        select(SKU)
        .where(SKU.id == sku_id)
        .options(selectinload(SKU.product))
    )
    sku = result.scalar_one_or_none()
    if sku is None:
        raise HTTPException(
            status_code=404,
            detail={"code": ErrorCode.NOT_FOUND, "message": "SKU not found"},
        )
    product = getattr(sku, "product", None) or await db.get(Product, sku.product_id)
    if product is None or getattr(product, "deleted", False):
        raise HTTPException(
            status_code=404,
            detail={"code": ErrorCode.NOT_FOUND, "message": "Product not found"},
        )
    return sku, product


def _reject_hard_blocked(product: Product) -> None:
    if product.status == ProductStatus.HARD_BLOCKED:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PRODUCT_HARD_BLOCKED",
                "message": "Product is HARD_BLOCKED — modification is forbidden",
            },
        )


@router.get("/skus/{sku_id}", response_model=SKUResponse)
async def get_sku(sku_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SKU)
        .where(SKU.id == sku_id)
        .options(
            selectinload(SKU.characteristics),
            selectinload(SKU.images),
        )
    )
    sku = result.scalar_one_or_none()
    if not sku:
        raise HTTPException(
            status_code=404,
            detail={"code": ErrorCode.NOT_FOUND, "message": "SKU not found"},
        )
    return SKUResponse.from_orm_sku(sku)



@router.patch("/skus/{sku_id}", response_model=SKUResponse)
async def update_sku(
    sku_id: UUID,
    body: SKUUpdate,
    db: AsyncSession = Depends(get_db),
):
    """PATCH /api/v1/skus/{id} — OpenAPI (HARD_BLOCKED → 403)."""
    sku, product = await _load_sku_product_or_404(sku_id, db)
    _reject_hard_blocked(product)
    data = body.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(sku, field, value)
    await db.commit()
    await db.refresh(sku)
    return SKUResponse.from_orm_sku(sku)


@router.delete("/skus/{sku_id}", status_code=204)
async def delete_sku(sku_id: UUID, db: AsyncSession = Depends(get_db)):
    """DELETE /api/v1/skus/{id} — HARD_BLOCKED → 403."""
    sku, product = await _load_sku_product_or_404(sku_id, db)
    _reject_hard_blocked(product)
    await db.delete(sku)
    await db.commit()
    return None


@router.post("/skus/{sku_id}/images", status_code=201)
async def add_sku_image(
    sku_id: UUID,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """POST /api/v1/skus/{id}/images — HARD_BLOCKED → 403."""
    sku, product = await _load_sku_product_or_404(sku_id, db)
    _reject_hard_blocked(product)
    url = str(body.get("url") or "")
    if not url:
        raise HTTPException(
            status_code=400,
            detail={"code": ErrorCode.VALIDATION_ERROR, "message": "url is required"},
        )
    img = SKUImage(sku_id=sku_id, url=url, ordering=int(body.get("ordering") or 0))
    db.add(img)
    await db.commit()
    await db.refresh(img)
    return {"id": str(img.id), "url": img.url, "ordering": img.ordering}


@router.patch("/skus/images/{image_id}")
@router.delete("/skus/images/{image_id}", status_code=204)
async def mutate_sku_image(
    image_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """PATCH/DELETE SKU image — HARD_BLOCKED parent product → 403."""
    img = await db.get(SKUImage, image_id)
    if img is None:
        raise HTTPException(
            status_code=404,
            detail={"code": ErrorCode.NOT_FOUND, "message": "Image not found"},
        )
    sku, product = await _load_sku_product_or_404(img.sku_id, db)
    _reject_hard_blocked(product)
    if request.method == "DELETE":
        await db.delete(img)
        await db.commit()
        return None
    return {"id": str(img.id), "url": img.url, "ordering": img.ordering}


@router.post("/skus/{sku_id}/reserve")
async def reserve_sku(
    sku_id: UUID, quantity: int, db: AsyncSession = Depends(get_db)
):
    sku = await db.get(SKU, sku_id)
    if not sku:
        raise HTTPException(
            status_code=404,
            detail={"code": ErrorCode.NOT_FOUND, "message": "SKU not found"},
        )
    if sku.active_quantity < quantity:
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.VALIDATION_ERROR,
                "message": (
                    f"Insufficient quantity. Available: {sku.active_quantity}, "
                    f"requested: {quantity}"
                ),
            },
        )
    sku.active_quantity -= quantity
    sku.blocked_quantity += quantity
    await db.commit()
    return {
        "sku_id": str(sku_id),
        "reserved": quantity,
        "remaining": sku.active_quantity,
    }


@router.post("/skus/{sku_id}/release")
async def release_sku(
    sku_id: UUID, quantity: int, db: AsyncSession = Depends(get_db)
):
    sku = await db.get(SKU, sku_id)
    if not sku:
        raise HTTPException(
            status_code=404,
            detail={"code": ErrorCode.NOT_FOUND, "message": "SKU not found"},
        )
    if sku.blocked_quantity < quantity:
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.VALIDATION_ERROR,
                "message": (
                    f"Cannot release more than reserved. "
                    f"Reserved: {sku.blocked_quantity}"
                ),
            },
        )
    sku.blocked_quantity -= quantity
    sku.active_quantity += quantity
    await db.commit()
    return {
        "sku_id": str(sku_id),
        "released": quantity,
        "active": sku.active_quantity,
    }


@router.post(
    "/inventory/reserve",
    response_model=ReserveResponse,
    status_code=200,
)
async def inventory_reserve(
    body: ReserveRequest,
    x_service_key: str | None = Header(None, alias="X-Service-Key"),
    db: AsyncSession = Depends(get_db),
):
    """
    POST /api/v1/inventory/reserve — OpenAPI published path (US-B2B-08).

    All-or-nothing reservation. Idempotent by idempotency_key.
    409 details.failed_items lists problematic SKUs.
    """
    return await inventory_service.reserve_skus(
        body, db, x_service_key=x_service_key
    )


@router.post(
    "/inventory/unreserve",
    response_model=UnreserveResponse,
    status_code=200,
)
async def inventory_unreserve(
    body: UnreserveRequest,
    x_service_key: str | None = Header(None, alias="X-Service-Key"),
    db: AsyncSession = Depends(get_db),
):
    """
    POST /api/v1/inventory/unreserve — OpenAPI published path.

    Idempotent by order_id (repeat call returns cached success, no stock change).
    """
    return await inventory_service.unreserve_skus(
        body, db, x_service_key=x_service_key
    )


# ── B2B-8: batch reserve / unreserve (canon paths) ──────────────────────


@router.post("/reserve", response_model=ReserveResponse, status_code=200)
async def reserve(
    body: ReserveRequest,
    x_service_key: str | None = Header(None, alias="X-Service-Key"),
    db: AsyncSession = Depends(get_db),
):
    """
    POST /api/v1/reserve — all-or-nothing SKU reservation (US-B2B-08).

    Idempotent by body.idempotency_key. On partial shortage → 409 + full rollback.
    When active_quantity hits 0 → SKU_OUT_OF_STOCK event to B2C.
    """
    return await inventory_service.reserve_skus(
        body, db, x_service_key=x_service_key
    )


@router.post("/unreserve", response_model=UnreserveResponse, status_code=200)
async def unreserve(
    body: UnreserveRequest,
    x_service_key: str | None = Header(None, alias="X-Service-Key"),
    db: AsyncSession = Depends(get_db),
):
    """POST /api/v1/unreserve — compensating release of reserved stock."""
    return await inventory_service.unreserve_skus(
        body, db, x_service_key=x_service_key
    )
