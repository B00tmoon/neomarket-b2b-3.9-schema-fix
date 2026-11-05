"""Schemas for POST /api/v1/reserve and POST /api/v1/unreserve (B2B-8 / OpenAPI)."""

from __future__ import annotations

from datetime import datetime
from typing import List
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InventoryItem(BaseModel):
    """OpenAPI InventoryItem — sku_id + quantity >= 1."""

    sku_id: UUID
    quantity: int = Field(..., ge=1)


class ReserveRequest(BaseModel):
    """OpenAPI ReserveRequest: idempotency_key, order_id, items."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: UUID
    order_id: UUID
    items: List[InventoryItem] = Field(..., min_length=1)


class ReserveResponse(BaseModel):
    """OpenAPI ReserveResponse."""

    order_id: UUID
    status: str = "RESERVED"
    reserved_at: datetime


class UnreserveRequest(BaseModel):
    """OpenAPI InventoryOrderRequest used for unreserve."""

    model_config = ConfigDict(extra="forbid")

    order_id: UUID
    items: List[InventoryItem] = Field(..., min_length=1)


class UnreserveResponse(BaseModel):
    """OpenAPI InventoryOrderResponse for unreserve."""

    order_id: UUID
    status: str = "UNRESERVED"
    processed_at: datetime


# Legacy aliases for internal/test compatibility
ReserveItem = InventoryItem
UnreserveItem = InventoryItem


class ReserveItemResult(BaseModel):
    sku_id: UUID
    quantity: int
    active_quantity: int
    reserved_quantity: int


class FailedReserveItem(BaseModel):
    sku_id: UUID
    requested: int
    available: int
    reason: str = "INSUFFICIENT_STOCK"
