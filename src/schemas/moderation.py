"""Pydantic schemas for moderation events (B2B-9 / OpenAPI ModerationEventRequest)."""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FieldReportIn(BaseModel):
    """OpenAPI FieldReport — required: field_name, comment."""

    model_config = ConfigDict(extra="forbid")

    field_name: str = Field(..., min_length=1)
    sku_id: Optional[UUID] = None
    comment: str = Field(..., min_length=1)


class ModerationEventRequest(BaseModel):
    """
    OpenAPI ModerationEventRequest — strict, no alias reconstruction.

    required: idempotency_key, product_id, event_type, occurred_at
    event_type ∈ {MODERATED, BLOCKED}
    blocking_reason_id is required when event_type == BLOCKED

    Foreign keys (status, blocking_reason, …) are rejected via extra="forbid"
    and never used to fill missing required fields.
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key: UUID
    product_id: UUID
    event_type: Literal["MODERATED", "BLOCKED"]
    occurred_at: datetime
    moderator_id: Optional[UUID] = None
    moderator_comment: Optional[str] = None
    blocking_reason_id: Optional[UUID] = None
    hard_block: bool = False
    field_reports: Optional[List[FieldReportIn]] = None

    @model_validator(mode="after")
    def require_reason_on_blocked(self) -> "ModerationEventRequest":
        if self.event_type == "BLOCKED" and self.blocking_reason_id is None:
            raise ValueError(
                "blocking_reason_id is required when event_type is BLOCKED"
            )
        return self

    @property
    def status_value(self) -> str:
        return self.event_type

    def resolved_field_reports(self) -> list[dict]:
        out: list[dict] = []
        for item in self.field_reports or []:
            out.append(
                {
                    "field_name": item.field_name,
                    "sku_id": str(item.sku_id) if item.sku_id else None,
                    "comment": item.comment,
                }
            )
        return out


class ModerationEventResponse(BaseModel):
    """Internal — OpenAPI success response is 204 No Content."""

    product_id: UUID
    status: str
    applied: bool = True


class ProductApproveRequest(BaseModel):
    comment: Optional[str] = None


class ProductApproveResponse(BaseModel):
    product_id: UUID
    status: str
    seller_id: UUID
    approved_at: datetime
    comment: Optional[str] = None
