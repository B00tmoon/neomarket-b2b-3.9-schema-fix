from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProductStatusEnum(str, Enum):
    CREATED = "CREATED"
    ON_MODERATION = "ON_MODERATION"
    MODERATED = "MODERATED"
    BLOCKED = "BLOCKED"
    HARD_BLOCKED = "HARD_BLOCKED"


class ProductImageCreate(BaseModel):
    """OpenAPI ProductImageCreate: required [url]; ordering default 0."""

    url: str = Field(..., description="URL of already uploaded image")
    ordering: int = Field(0, ge=0, description="Display order (0 = main photo)")


class ProductImageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    url: str
    ordering: int = 0


class ProductCharacteristicBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    value: str = Field(..., min_length=1, max_length=500)


class ProductCharacteristicCreate(ProductCharacteristicBase):
    pass


class ProductCharacteristicResponse(ProductCharacteristicBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID


class SKUCharacteristicValue(BaseModel):
    """OpenAPI Characteristic — required name, value (no extra length limits)."""

    name: str
    value: str


class CharacteristicResponse(BaseModel):
    """Characteristic in responses — includes id per protocols."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    value: str


class SKUImageCreate(BaseModel):
    """OpenAPI SKUImageCreate: required [url]; ordering default 0."""

    url: str
    ordering: int = Field(0, description="Display order (default 0)")


class SKUImageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    url: str
    ordering: int = 0


class SKUCreate(BaseModel):
    """Request body for POST /api/v1/skus — OpenAPI SKUCreate.

    required: product_id, name, price
    optional: discount (default 0), cost_price (nullable), article (nullable),
              images (default []), characteristics (default [])
    """

    model_config = ConfigDict(extra="forbid")

    product_id: UUID = Field(..., description="Product this SKU belongs to")
    name: str = Field(..., min_length=1, max_length=255)
    price: int = Field(..., ge=0, description="Sale price in kopecks (>= 0)")
    discount: int = Field(0, ge=0, description="Absolute discount in kopecks")
    cost_price: Optional[int] = Field(
        None, description="Cost price in kopecks (seller-only, optional, nullable)"
    )
    article: Optional[str] = Field(None, description="Article / vendor code (nullable)")
    images: List[SKUImageCreate] = Field(default_factory=list)
    characteristics: List[SKUCharacteristicValue] = Field(default_factory=list)


class SKUUpdate(BaseModel):
    """Partial SKU update (OpenAPI PATCH /skus/{sku_id})."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    price: Optional[int] = Field(None, ge=0)
    discount: Optional[int] = Field(None, ge=0)
    cost_price: Optional[int] = None
    article: Optional[str] = None


class SKUResponse(BaseModel):
    """Full seller-view SKUResponse per neomarket-protocols."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    name: str
    price: int
    discount: int = 0
    cost_price: Optional[int] = None
    stock_quantity: int = 0
    active_quantity: int = 0
    reserved_quantity: int = 0
    article: Optional[str] = None
    images: List[SKUImageResponse] = []
    characteristics: List[CharacteristicResponse] = []
    created_at: datetime
    updated_at: Optional[datetime] = None

    @classmethod
    def from_orm_sku(cls, sku: object) -> "SKUResponse":
        chars = [
            CharacteristicResponse(id=c.id, name=c.name, value=c.value)
            for c in (getattr(sku, "characteristics", None) or [])
        ]
        imgs = [
            SKUImageResponse(id=img.id, url=img.url, ordering=int(img.ordering or 0))
            for img in (getattr(sku, "images", None) or [])
        ]
        # Fallback: legacy single image column
        if not imgs:
            legacy = getattr(sku, "image", None) or ""
            if legacy:
                imgs = [
                    SKUImageResponse(
                        id=getattr(sku, "id"),
                        url=str(legacy),
                        ordering=0,
                    )
                ]
        created = getattr(sku, "created_at", None) or datetime.now(timezone.utc)
        return cls(
            id=sku.id,
            product_id=sku.product_id,
            name=sku.name,
            price=int(sku.price),
            discount=int(getattr(sku, "discount", 0) or 0),
            cost_price=getattr(sku, "cost_price", None),
            stock_quantity=int(getattr(sku, "stock_quantity", 0) or 0),
            active_quantity=int(getattr(sku, "active_quantity", 0) or 0),
            reserved_quantity=int(getattr(sku, "blocked_quantity", 0) or 0),
            article=getattr(sku, "article", None),
            images=imgs,
            characteristics=chars,
            created_at=created,
            updated_at=getattr(sku, "updated_at", None),
        )



class SKUPublicResponse(BaseModel):
    """OpenAPI SKUPublicResponse — no cost_price / reserved_quantity."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    name: str
    price: int
    discount: int = 0
    stock_quantity: int = 0
    active_quantity: int = 0
    article: Optional[str] = None
    images: List[SKUImageResponse] = []
    characteristics: List[CharacteristicResponse] = []

    @classmethod
    def from_orm_sku(cls, sku: object) -> "SKUPublicResponse":
        chars = [
            CharacteristicResponse(id=c.id, name=c.name, value=c.value)
            for c in (getattr(sku, "characteristics", None) or [])
        ]
        imgs = [
            SKUImageResponse(id=img.id, url=img.url, ordering=int(img.ordering or 0))
            for img in (getattr(sku, "images", None) or [])
        ]
        if not imgs:
            legacy = getattr(sku, "image", None) or ""
            if legacy:
                imgs = [
                    SKUImageResponse(
                        id=getattr(sku, "id"),
                        url=str(legacy),
                        ordering=0,
                    )
                ]
        return cls(
            id=sku.id,
            product_id=sku.product_id,
            name=sku.name,
            price=int(sku.price),
            discount=int(getattr(sku, "discount", 0) or 0),
            stock_quantity=int(getattr(sku, "stock_quantity", 0) or 0),
            active_quantity=int(getattr(sku, "active_quantity", 0) or 0),
            article=getattr(sku, "article", None),
            images=imgs,
            characteristics=chars,
        )


class ProductPublicResponse(BaseModel):
    """
    OpenAPI ProductPublicResponse — inter-service / B2C card.

    Explicitly excludes seller-sensitive SKU fields (cost_price, reserved_quantity)
    and moderation detail fields (blocking_reason, field_reports).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    seller_id: UUID
    title: str
    description: str
    category_id: UUID
    status: str
    slug: str = ""
    images: List[ProductImageResponse] = []
    characteristics: List[ProductCharacteristicResponse] = []
    skus: List[SKUPublicResponse] = []
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ProductPublicShortResponse(BaseModel):
    """
    OpenAPI ProductPublicShortResponse — catalog list item for B2C.

    required: id, title, slug, status, category_id, created_at, min_price
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    slug: str = ""
    status: str
    category_id: UUID
    min_price: int
    cover_image: Optional[str] = None
    created_at: Optional[datetime] = None


class CatalogListResponse(BaseModel):
    """Paginated B2C catalog (OpenAPI ProductPublicPaginatedResponse)."""

    items: List[ProductPublicShortResponse]
    total_count: int
    limit: int
    offset: int


class ProductCreate(BaseModel):
    """Request body for POST /api/v1/products — OpenAPI ProductCreate.

    required: title, description, category_id
    optional: slug (nullable), images (default []), characteristics (default [])

    seller_id is NEVER accepted from body (JWT / X-Seller-Id).
    skus are NEVER accepted here — use POST /api/v1/skus (B2B-2).
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Product title, 1-255 characters",
    )
    description: str = Field(
        ...,
        min_length=1,
        max_length=5000,
        description="Product description, 1-5000 characters",
    )
    category_id: UUID = Field(..., description="Category ID (UUID) is required")
    slug: Optional[str] = Field(
        None,
        description="Optional client-provided slug; server may generate if omitted",
    )
    images: List[ProductImageCreate] = Field(
        default_factory=list,
        description="Product images (optional; default empty list)",
    )
    characteristics: List[ProductCharacteristicCreate] = Field(
        default_factory=list,
        description="Product characteristics (optional; default empty list)",
    )


class ProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, min_length=1, max_length=5000)
    category_id: Optional[UUID] = None


class BlockingReasonResponse(BaseModel):
    """Blocking reason shown to seller when status is BLOCKED (B2B-5 / protocols)."""

    id: UUID
    title: str
    comment: str


class FieldReportResponse(BaseModel):
    """Per-field moderation remark for the seller."""

    field_name: str
    sku_id: Optional[UUID] = None
    comment: str


class ProductResponse(BaseModel):
    """Full product response (seller detail view / create response).

    B2B-5 / ProductDetailResponse: includes blocked, blocking_reason, field_reports.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    seller_id: UUID
    title: str
    description: str
    category_id: UUID
    status: str
    slug: str
    images: List[ProductImageResponse]
    characteristics: List[ProductCharacteristicResponse] = []
    skus: List[SKUResponse] = []
    created_at: datetime
    updated_at: Optional[datetime] = None
    deleted: bool = False
    blocked: bool = False
    blocking_comment: Optional[str] = None
    blocking_reason_id: Optional[UUID] = None
    moderator_comment: Optional[str] = None
    blocking_reason: Optional[BlockingReasonResponse] = None
    field_reports: List[FieldReportResponse] = Field(default_factory=list)

    @field_validator("field_reports", mode="before")
    @classmethod
    def _coerce_field_reports(cls, v):
        return v if v is not None else []

    @field_validator("blocked", mode="before")
    @classmethod
    def _coerce_blocked(cls, v):
        return bool(v) if v is not None else False
