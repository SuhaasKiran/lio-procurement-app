import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RequestStatus = Literal["open", "in_progress", "closed"]
ApprovalStatus = Literal["pending", "approved", "rejected"]


# ---------------------------------------------------------------------------
# Inputs (camelCase — matches what the frontend forms already send)
# ---------------------------------------------------------------------------

class OrderLineIn(BaseModel):
    position_description: str = Field(min_length=1, alias="positionDescription")
    unit_price: float = Field(ge=0, alias="unitPrice")
    amount: float = Field(ge=0)
    unit: str = Field(min_length=1)
    total_price: float = Field(ge=0, alias="totalPrice")
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern="^[A-Za-z]{3}$")
    catalog_article_id: uuid.UUID | None = Field(default=None, alias="catalogArticleId")
    catalog_article_number: str | None = Field(default=None, alias="catalogArticleNumber")

    @model_validator(mode="after")
    def normalize_currency(self) -> "OrderLineIn":
        self.currency = self.currency.upper()
        return self


class AdjustmentIn(BaseModel):
    """A positive tax or discount applied to a line or the whole quote."""

    kind: Literal["tax", "discount", "charge"]
    description: str = ""
    amount: float = Field(ge=0)
    scope: Literal["line", "order"] = "order"
    line_index: int | None = Field(default=None, alias="lineIndex")

    @model_validator(mode="after")
    def validate_scope(self) -> "AdjustmentIn":
        if self.scope == "line" and self.line_index is None:
            raise ValueError("A line adjustment must identify an order line")
        if self.scope == "order" and self.line_index is not None:
            raise ValueError("An order adjustment cannot identify an order line")
        return self


class FinancialBreakdownIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    adjustments: list[AdjustmentIn] = Field(default_factory=list)


class RequestCreate(BaseModel):
    requestor_name: str = Field(min_length=1, alias="requestorName")
    title: str = Field(min_length=1, alias="titleShortDescription")
    vendor_name: str = Field(default="", alias="vendorName")
    vat_id: str | None = Field(default=None, alias="vatId")
    commodity_group_id: int = Field(alias="commodityGroupId")
    total_cost: float = Field(ge=0, alias="totalCost")
    source_quote_currency: str | None = Field(
        default=None,
        alias="sourceQuoteCurrency",
        min_length=3,
        max_length=3,
        pattern="^[A-Za-z]{3}$",
    )

    department: str | None = None
    order_lines: list[OrderLineIn] = Field(alias="orderLines", min_length=1)
    financial_breakdown: FinancialBreakdownIn = Field(
        default_factory=FinancialBreakdownIn, alias="financialBreakdown"
    )
    discrepancy_note: str | None = Field(default=None, alias="discrepancyNote")

    @model_validator(mode="after")
    def normalize_source_quote_currency(self) -> "RequestCreate":
        if self.source_quote_currency:
            self.source_quote_currency = self.source_quote_currency.upper()
        return self


class RequestUpdate(BaseModel):
    requestor_name: str = Field(min_length=1, alias="requestorName")
    title: str = Field(min_length=1)
    vendor_name: str = Field(min_length=1, alias="vendorName")
    vat_id: str | None = Field(default=None, alias="vatId")
    commodity_group_id: int = Field(alias="commodityGroupId")
    total_cost: float = Field(ge=0, alias="totalCost")
    department: str | None = None
    order_lines: list[OrderLineIn] = Field(alias="orderLines")
    financial_breakdown: FinancialBreakdownIn = Field(
        default_factory=FinancialBreakdownIn, alias="financialBreakdown"
    )
    discrepancy_note: str | None = Field(default=None, alias="discrepancyNote")


class StatusUpdate(BaseModel):
    status: RequestStatus


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]


# ---------------------------------------------------------------------------
# Outputs (snake_case — matches the DB row shapes the frontend renders)
# ---------------------------------------------------------------------------

class CommodityGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    name: str


class OrderLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    position_description: str
    catalog_article_id: uuid.UUID | None
    catalog_article_number: str | None
    unit_price: float
    amount: float
    unit: str
    total_price: float
    line_order: int
    created_at: datetime


class RequestActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    summary: str
    detail: dict | None
    created_at: datetime


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID | None
    created_by: uuid.UUID | None
    requestor_name: str
    title: str
    vendor_name: str
    supplier_id: uuid.UUID | None
    currency: str
    vat_id: str | None
    commodity_group_id: int | None
    department: str | None
    status: RequestStatus
    approval_status: ApprovalStatus
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    total_cost: float | None
    financial_breakdown: dict | None
    discrepancy_note: str | None
    created_at: datetime
    updated_at: datetime
    order_lines: list[OrderLineOut]
    # Serialized as ``commodity_groups`` (the join key the frontend renders) but
    # read from the ORM's ``commodity_group`` relationship.
    commodity_groups: CommodityGroupOut | None = Field(
        default=None, validation_alias="commodity_group"
    )
