from __future__ import annotations

from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.base import get_db
from src.models.product import Product, ProductBlockingReason, ProductCharacteristic, ProductImage, ProductStatus
from src.models.sku import SKU, SKUCharacteristic
from src.schemas.errors import ErrorCode
from src.schemas.product import (
    BlockingReasonResponse,
    CatalogListResponse,
    FieldReportResponse,
    ProductCreate,
    ProductPublicResponse,
    ProductPublicShortResponse,
    ProductResponse,
    ProductStatusEnum,
    ProductUpdate,
    SKUPublicResponse,
    SKUResponse,
)
from src.settings import settings

router = APIRouter()


def _parse_seller_id(request: Request) -> UUID | None:
    """Extract seller_id from JWT / request.state.user / X-Seller-Id as UUID."""
    seller_id = getattr(request.state, "user", None)
    if seller_id is None:
        seller_id = request.headers.get("X-Seller-Id")
    if seller_id is None:
        return None
    try:
        return UUID(str(seller_id))
    except (ValueError, TypeError, AttributeError):
        return None


def _check_hard_blocked(product: Product) -> None:
    """Raise 403 if product is HARD_BLOCKED."""
    if product.status == ProductStatus.HARD_BLOCKED:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PRODUCT_HARD_BLOCKED",
                "message": "Product is HARD_BLOCKED — modification is forbidden",
                "details": {"field": "status"},
            },
        )


@router.post("/products", response_model=ProductResponse, status_code=201)
async def create_product(
    product_data: ProductCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ProductResponse:
    """
    Create a product card only (US-B2B-01 / B2B-1).

    seller_id is taken from JWT / request.state.user / X-Seller-Id — never from body.
    Body (OpenAPI ProductCreate): title, description, category_id required;
    optional characteristics. SKUs are NOT accepted and NOT created
    (use POST /api/v1/skus). On success: status=CREATED, skus=[], HTTP 201.
    """
    seller_id = _parse_seller_id(request)
    if seller_id is None:
        raw = getattr(request.state, "user", None) or request.headers.get("X-Seller-Id")
        if raw is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "UNAUTHORIZED",
                    "message": "JWT token required",
                },
            )
        raise HTTPException(
            status_code=401,
            detail={
                "code": "UNAUTHORIZED",
                "message": "Invalid JWT seller_id (must be UUID)",
            },
        )

    from src.cruds.products import create_product as crud_create_product

    db_product = await crud_create_product(product_data, seller_id, db)
    return db_product


def _parse_ids_param(ids: Optional[str]) -> list[UUID] | None:
    if not ids:
        return None
    id_list: list[UUID] = []
    for part in ids.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            id_list.append(UUID(part))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": ErrorCode.VALIDATION_ERROR,
                    "message": f"Invalid UUID: {part}",
                },
            )
    return id_list


def _to_public_product(product: Product) -> ProductPublicResponse:
    skus = [SKUPublicResponse.from_orm_sku(s) for s in (product.skus or [])]
    return ProductPublicResponse(
        id=product.id,
        seller_id=product.seller_id,
        title=product.title,
        description=product.description or "",
        category_id=product.category_id,
        status=(
            product.status.value
            if hasattr(product.status, "value")
            else str(product.status)
        ),
        slug=product.slug or "",
        images=list(product.images or []),
        characteristics=list(product.characteristics or []),
        skus=skus,
        created_at=getattr(product, "created_at", None),
        updated_at=getattr(product, "updated_at", None),
    )


def _to_catalog_short(product: Product) -> ProductPublicShortResponse:
    """Catalog list item with required min_price (OpenAPI ProductPublicShortResponse)."""
    prices = [
        int(s.price)
        for s in (product.skus or [])
        if getattr(s, "active_quantity", 0) and int(s.active_quantity) > 0
    ]
    if not prices:
        prices = [int(s.price) for s in (product.skus or [])]
    min_price = min(prices) if prices else 0

    cover = None
    images = list(product.images or [])
    if images:
        ordered = sorted(images, key=lambda i: int(getattr(i, "ordering", 0) or 0))
        cover = ordered[0].url if ordered else None

    status_val = (
        product.status.value
        if hasattr(product.status, "value")
        else str(product.status)
    )
    return ProductPublicShortResponse(
        id=product.id,
        title=product.title,
        slug=product.slug or "",
        status=status_val,
        category_id=product.category_id,
        min_price=min_price,
        cover_image=cover,
        created_at=getattr(product, "created_at", None),
    )


def _parse_characteristic_filters(request: Request) -> dict[str, list[str]]:
    """Parse OpenAPI deepObject filters[name]=value query params."""
    out: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        name: str | None = None
        if key.startswith("filters[") and key.endswith("]"):
            name = key[len("filters[") : -1]
        elif key.startswith("filters."):
            name = key[len("filters.") :]
        if not name:
            continue
        name = name.strip()
        if not name:
            continue
        out.setdefault(name, []).append(str(value))
    return out


@router.get("/products")
async def list_products(
    request: Request,
    seller_id: Optional[UUID] = Query(None, description="Фильтр по продавцу"),
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
    status: Optional[ProductStatusEnum] = Query(None, description="Фильтр по статусу"),
    category_id: Optional[UUID] = Query(None, description="Фильтр по категории"),
    category: Optional[UUID] = Query(
        None, description="Alias for category_id (canon catalog)"
    ),
    ids: Optional[str] = Query(
        None, description="Batch IDs for B2C catalog, comma-separated UUIDs"
    ),
    search: Optional[str] = Query(
        None, description="Text search by title/description (catalog)"
    ),
    sort: Optional[str] = Query(
        None,
        description="Catalog sort: price_asc | price_desc | created_desc | popular",
    ),
    min_price: Optional[int] = Query(
        None, ge=0, description="Min SKU price filter (kopecks)"
    ),
    max_price: Optional[int] = Query(
        None, ge=0, description="Max SKU price filter (kopecks)"
    ),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    limit: Optional[int] = Query(None, ge=1, le=100),
    offset: Optional[int] = Query(None, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /api/v1/products — dual mode (B2B-7 + seller list).

    Catalog mode (X-Service-Key == b2c_to_b2b_key):
      - no seller JWT required
      - visibility: status=MODERATED, deleted=false, ≥1 SKU with active_quantity>0
      - HARD_BLOCKED excluded (not MODERATED)
      - search / category / price filters / sort
      - response: CatalogListResponse with min_price, no cost_price / reserved_quantity

    Seller mode (JWT / X-Seller-Id, no service key):
      - own products (or filter by seller_id query)
      - full ProductResponse list
    """
    service_key = x_service_key or request.headers.get("X-Service-Key")
    is_catalog_mode = bool(service_key)

    if is_catalog_mode:
        if service_key != settings.b2c_to_b2b_key:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": ErrorCode.UNAUTHORIZED,
                    "message": "Invalid or missing X-Service-Key",
                },
            )
        return await _list_catalog(
            db=db,
            category_id=category_id or category,
            seller_id=seller_id,
            characteristic_filters=_parse_characteristic_filters(request),
            ids=_parse_ids_param(ids),
            search=search,
            sort=sort,
            min_price=min_price,
            max_price=max_price,
            limit=limit if limit is not None else size,
            offset=offset if offset is not None else (page - 1) * size,
        )

    # Seller mode — require seller identity
    parsed_seller = _parse_seller_id(request)
    effective_seller = seller_id or parsed_seller
    if effective_seller is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": ErrorCode.UNAUTHORIZED,
                "message": "X-Service-Key or seller JWT required",
            },
        )

    query = select(Product).where(Product.deleted == False)  # noqa: E712
    query = query.where(Product.seller_id == effective_seller)
    if status:
        query = query.where(Product.status == ProductStatus(status.value))
    if category_id:
        query = query.where(Product.category_id == category_id)
    id_list = _parse_ids_param(ids)
    if id_list:
        query = query.where(Product.id.in_(id_list))

    query = query.options(
        selectinload(Product.images),
        selectinload(Product.characteristics),
        selectinload(Product.skus).selectinload(SKU.characteristics),
        selectinload(Product.skus).selectinload(SKU.images),
    )
    query = query.offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    return list(result.scalars().all())


async def _list_catalog(
    *,
    db: AsyncSession,
    category_id: Optional[UUID],
    seller_id: Optional[UUID],
    characteristic_filters: dict[str, list[str]] | None,
    ids: list[UUID] | None,
    search: Optional[str],
    sort: Optional[str],
    min_price: Optional[int],
    max_price: Optional[int],
    limit: int,
    offset: int,
) -> CatalogListResponse:
    """
    B2C catalog: MODERATED + not deleted + in-stock SKU.

    Full OpenAPI public-list filters:
      category_id, search, min_price, max_price, seller_id,
      filters[char_name]=value (characteristics),
      sort: price_asc | price_desc | created_desc | popular,
      ids, limit, offset.
    """
    in_stock = exists(
        select(SKU.id).where(
            SKU.product_id == Product.id,
            SKU.active_quantity > 0,
        )
    )
    filters = [
        Product.deleted == False,  # noqa: E712
        Product.status == ProductStatus.MODERATED,
        in_stock,
    ]
    if category_id is not None:
        filters.append(Product.category_id == category_id)
    if seller_id is not None:
        filters.append(Product.seller_id == seller_id)
    if ids:
        filters.append(Product.id.in_(ids))

    if search and search.strip():
        term = f"%{search.strip()}%"
        filters.append(
            (Product.title.ilike(term)) | (Product.description.ilike(term))
        )

    if min_price is not None:
        filters.append(
            exists(
                select(SKU.id).where(
                    SKU.product_id == Product.id,
                    SKU.active_quantity > 0,
                    SKU.price >= min_price,
                )
            )
        )
    if max_price is not None:
        filters.append(
            exists(
                select(SKU.id).where(
                    SKU.product_id == Product.id,
                    SKU.active_quantity > 0,
                    SKU.price <= max_price,
                )
            )
        )

    # filters[name]=value — AND across names, OR within values for one name.
    # Match product-level OR SKU-level characteristics (OpenAPI memory/color filters).
    if characteristic_filters:
        for char_name, values in characteristic_filters.items():
            cleaned = [str(v).strip() for v in values if str(v).strip()]
            if not cleaned:
                continue
            if len(cleaned) == 1:
                product_value_match = ProductCharacteristic.value.ilike(cleaned[0])
                sku_value_match = SKUCharacteristic.value.ilike(cleaned[0])
            else:
                product_value_match = or_(
                    *[ProductCharacteristic.value.ilike(v) for v in cleaned]
                )
                sku_value_match = or_(
                    *[SKUCharacteristic.value.ilike(v) for v in cleaned]
                )
            on_product = exists(
                select(ProductCharacteristic.id).where(
                    ProductCharacteristic.product_id == Product.id,
                    ProductCharacteristic.name.ilike(char_name),
                    product_value_match,
                )
            )
            on_sku = exists(
                select(SKUCharacteristic.id)
                .select_from(SKUCharacteristic)
                .join(SKU, SKU.id == SKUCharacteristic.sku_id)
                .where(
                    SKU.product_id == Product.id,
                    SKUCharacteristic.name.ilike(char_name),
                    sku_value_match,
                )
            )
            filters.append(or_(on_product, on_sku))

    count_q = select(func.count()).select_from(Product).where(*filters)
    total = (await db.execute(count_q)).scalar_one()

    min_sku_price = (
        select(func.min(SKU.price))
        .where(SKU.product_id == Product.id, SKU.active_quantity > 0)
        .correlate(Product)
        .scalar_subquery()
    )

    query = (
        select(Product)
        .where(*filters)
        .options(
            selectinload(Product.images),
            selectinload(Product.characteristics),
            selectinload(Product.skus).selectinload(SKU.characteristics),
            selectinload(Product.skus).selectinload(SKU.images),
        )
    )

    sort_key = (sort or "created_desc").strip().lower()
    if sort_key in ("date_desc", "created_desc"):
        query = query.order_by(Product.created_at.desc().nulls_last())
    elif sort_key == "price_asc":
        query = query.order_by(min_sku_price.asc().nulls_last())
    elif sort_key == "price_desc":
        query = query.order_by(min_sku_price.desc().nulls_last())
    elif sort_key == "popular":
        stock_sum = (
            select(func.coalesce(func.sum(SKU.active_quantity), 0))
            .where(SKU.product_id == Product.id)
            .correlate(Product)
            .scalar_subquery()
        )
        query = query.order_by(
            stock_sum.desc().nulls_last(),
            Product.created_at.desc().nulls_last(),
        )
    else:
        query = query.order_by(Product.created_at.desc().nulls_last())

    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    products = list(result.scalars().all())
    items = [_to_catalog_short(p) for p in products]
    return CatalogListResponse(
        items=items,
        total_count=int(total),
        limit=limit,
        offset=offset,
    )



@router.post("/products/batch")
async def get_products_batch(
    product_ids: list[UUID], db: AsyncSession = Depends(get_db)
):
    """Получить несколько товаров по списку UUID (batch запрос для B2C)."""
    products = await db.execute(select(Product).where(Product.id.in_(product_ids)))
    product_list = products.scalars().all()

    result = {}
    for product in product_list:
        min_price = min([sku.price for sku in product.skus], default=0)
        result[str(product.id)] = {
            "product_id": str(product.id),
            "title": product.title,
            "description": product.description,
            "category_id": str(product.category_id),
            "status": product.status.value,
            "main_image_url": product.images[0].url if product.images else "",
            "images": [img.url for img in product.images],
            "min_price": min_price,
            "is_available": product.status == ProductStatus.MODERATED
            and not product.deleted,
            "characteristics": {
                char.name: char.value for char in product.characteristics
            },
            "skus": [
                {
                    "sku_id": str(sku.id),
                    "sku_code": sku.sku_code,
                    "name": sku.name,
                    "price": sku.price,
                    "active_quantity": sku.active_quantity,
                    "blocked_quantity": sku.blocked_quantity,
                    "active": sku.active,
                    "characteristics": [
                        {"name": c.name, "value": c.value} for c in sku.characteristics
                    ],
                }
                for sku in product.skus
            ],
        }
    return result


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": ErrorCode.NOT_FOUND,
            "message": "Product not found",
        },
    )


def _status_str(product: Product) -> str:
    return (
        product.status.value
        if hasattr(product.status, "value")
        else str(product.status)
    )


async def _build_product_detail(product: Product, db: AsyncSession) -> ProductResponse:
    """Seller-view: full payload with cost_price/reserved_quantity + blocking data."""
    status_val = _status_str(product)
    blocked = status_val in (
        ProductStatus.BLOCKED.value,
        ProductStatus.HARD_BLOCKED.value,
    )

    blocking_reason: BlockingReasonResponse | None = None
    if blocked:
        title = None
        reason_id = product.blocking_reason_id
        if reason_id is not None:
            br = await db.get(ProductBlockingReason, reason_id)
            if br is not None:
                title = br.name
        comment = product.blocking_comment or ""
        title = title or comment or "Blocked"
        blocking_reason = BlockingReasonResponse(
            id=reason_id or uuid4(),
            title=title,
            comment=comment,
        )

    field_reports: list[FieldReportResponse] = []
    raw_reports = product.field_reports or []
    if isinstance(raw_reports, list):
        for item in raw_reports:
            if not isinstance(item, dict):
                continue
            field_name = item.get("field_name") or item.get("field") or ""
            comment = item.get("comment") or ""
            if not field_name and not comment:
                continue
            sku_raw = item.get("sku_id")
            sku_id = None
            if sku_raw:
                try:
                    sku_id = UUID(str(sku_raw))
                except (ValueError, TypeError):
                    sku_id = None
            field_reports.append(
                FieldReportResponse(
                    field_name=str(field_name),
                    sku_id=sku_id,
                    comment=str(comment),
                )
            )

    skus = [SKUResponse.from_orm_sku(s) for s in (product.skus or [])]

    return ProductResponse(
        id=product.id,
        seller_id=product.seller_id,
        title=product.title,
        description=product.description or "",
        category_id=product.category_id,
        status=status_val,
        slug=product.slug or "",
        images=list(product.images or []),
        characteristics=list(product.characteristics or []),
        skus=skus,
        created_at=product.created_at,
        updated_at=product.updated_at,
        deleted=bool(product.deleted),
        blocked=blocked,
        blocking_comment=product.blocking_comment,
        blocking_reason_id=product.blocking_reason_id,
        moderator_comment=product.blocking_comment,
        blocking_reason=blocking_reason,
        field_reports=field_reports,
    )


def _build_product_public(product: Product) -> ProductPublicResponse:
    """
    Inter-service card (OpenAPI ProductPublicResponse).

    Uses SKUPublicResponse — physically cannot include cost_price / reserved_quantity.
    """
    skus = [SKUPublicResponse.from_orm_sku(s) for s in (product.skus or [])]
    return ProductPublicResponse(
        id=product.id,
        seller_id=product.seller_id,
        title=product.title,
        description=product.description or "",
        category_id=product.category_id,
        status=_status_str(product),
        slug=product.slug or "",
        images=list(product.images or []),
        characteristics=list(product.characteristics or []),
        skus=skus,
        created_at=getattr(product, "created_at", None),
        updated_at=getattr(product, "updated_at", None),
    )


def _is_valid_service_key(x_service_key: str | None) -> bool:
    """Accept B2C or Moderation inter-service keys."""
    if not x_service_key:
        return False
    return x_service_key in {
        settings.b2c_to_b2b_key,
        settings.b2b_to_mod_key,
    }


@router.get("/products/{product_id}")
async def get_product(
    product_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
):
    """
    GET /api/v1/products/{id} — B2B-5 view product card.

    Seller mode (JWT / X-Seller-Id):
      - own products only; foreign → 404 (not 403)
      - ProductDetailResponse / ProductResponse with cost_price, reserved_quantity,
        blocking_reason, field_reports

    Inter-service mode (valid X-Service-Key):
      - any product, no ownership check
      - ProductPublicResponse with SKUPublicResponse
        (no cost_price, no reserved_quantity)
    """
    seller_id = _parse_seller_id(request)
    service_key = x_service_key or request.headers.get("X-Service-Key")
    is_service = bool(service_key)

    if is_service and not _is_valid_service_key(service_key):
        raise HTTPException(
            status_code=401,
            detail={
                "code": ErrorCode.UNAUTHORIZED,
                "message": "Invalid X-Service-Key",
            },
        )

    result = await db.execute(
        select(Product)
        .where(Product.id == product_id)
        .options(
            selectinload(Product.images),
            selectinload(Product.characteristics),
            selectinload(Product.skus).selectinload(SKU.characteristics),
            selectinload(Product.skus).selectinload(SKU.images),
        )
    )
    product = result.scalar_one_or_none()

    if product is None or product.deleted:
        raise _not_found()

    if is_service:
        return _build_product_public(product)

    # Seller mode — IDOR: only own products
    if seller_id is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": ErrorCode.UNAUTHORIZED,
                "message": "JWT token required",
            },
        )
    if product.seller_id != seller_id:
        raise _not_found()

    return await _build_product_detail(product, db)


async def _load_product_or_404(product_id: UUID, db: AsyncSession) -> Product:
    db_product = await db.get(Product, product_id)
    if not db_product or db_product.deleted:
        raise HTTPException(
            status_code=404,
            detail={"code": ErrorCode.NOT_FOUND, "message": "Product not found"},
        )
    return db_product


@router.put("/products/{product_id}", response_model=ProductResponse)
@router.patch("/products/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: UUID,
    product_update: ProductUpdate,
    db: AsyncSession = Depends(get_db),
):
    """PATCH/PUT /api/v1/products/{id} — OpenAPI update (HARD_BLOCKED → 403)."""
    db_product = await _load_product_or_404(product_id, db)
    _check_hard_blocked(db_product)

    update_data = product_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_product, field, value)

    await db.commit()
    await db.refresh(db_product)
    return db_product


@router.delete("/products/{product_id}", status_code=204)
@router.post("/products/{product_id}/delete", status_code=204)
async def delete_product(product_id: UUID, db: AsyncSession = Depends(get_db)):
    """DELETE /api/v1/products/{id} — soft delete (HARD_BLOCKED → 403)."""
    db_product = await _load_product_or_404(product_id, db)
    _check_hard_blocked(db_product)
    db_product.deleted = True
    await db.commit()
    return None


@router.post("/products/{product_id}/images", status_code=201)
async def add_product_image(
    product_id: UUID,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """POST /api/v1/products/{id}/images — HARD_BLOCKED → 403."""
    db_product = await _load_product_or_404(product_id, db)
    _check_hard_blocked(db_product)
    url = str(body.get("url") or "")
    if not url:
        raise HTTPException(
            status_code=400,
            detail={"code": ErrorCode.VALIDATION_ERROR, "message": "url is required"},
        )
    img = ProductImage(
        product_id=product_id,
        url=url,
        ordering=int(body.get("ordering") or 0),
    )
    db.add(img)
    await db.commit()
    await db.refresh(img)
    return {"id": str(img.id), "url": img.url, "ordering": img.ordering}


@router.patch("/products/images/{image_id}")
@router.delete("/products/images/{image_id}", status_code=204)
async def mutate_product_image(
    image_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """PATCH/DELETE product image — HARD_BLOCKED parent → 403."""
    img = await db.get(ProductImage, image_id)
    if img is None:
        raise HTTPException(
            status_code=404,
            detail={"code": ErrorCode.NOT_FOUND, "message": "Image not found"},
        )
    product = await _load_product_or_404(img.product_id, db)
    _check_hard_blocked(product)
    if request.method == "DELETE":
        await db.delete(img)
        await db.commit()
        return None
    # PATCH: optional url/ordering from body is out of scope; forbid only
    return {"id": str(img.id), "url": img.url, "ordering": img.ordering}


@router.post("/products/{product_id}/submit-moderation", status_code=204)
async def submit_for_moderation(
    product_id: UUID, db: AsyncSession = Depends(get_db)
):
    """Отправить товар на модерацию (HARD_BLOCKED → 403)."""
    db_product = await _load_product_or_404(product_id, db)
    _check_hard_blocked(db_product)

    if db_product.status != ProductStatus.CREATED:
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.VALIDATION_ERROR,
                "message": f"Cannot submit product with status {db_product.status.value}",
            },
        )

    db_product.status = ProductStatus.ON_MODERATION
    await db.commit()
    return None
