from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _empty_numeric(value: object, default: int | float = 0) -> object:
    """Map absent/blank AI output to the schema's missing-value default."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    return value


class PdfParseResponse(BaseModel):
    text: str


class ExtractionRequest(BaseModel):
    text: str = Field(max_length=200_000)


class CategoryLineIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    position_description: str = Field(min_length=1, max_length=1000, alias="positionDescription")
    total_price: float = Field(ge=0, alias="totalPrice")
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern="^[A-Za-z]{3}$")

    @model_validator(mode="after")
    def normalize_currency(self) -> "CategoryLineIn":
        self.currency = self.currency.upper()
        return self


class CategoryAssignmentRequest(BaseModel):
    lines: list[CategoryLineIn] = Field(min_length=1, max_length=50)


class CategoryAssignmentOut(BaseModel):
    index: int
    commodity_group_id: int | None = Field(alias="commodityGroupId")
    status: Literal["assigned", "needs_review"]
    error: str | None = None


class CategoryAssignmentResponse(BaseModel):
    lines: list[CategoryAssignmentOut]


# camelCase on the wire — matches the AI's JSON output format and what the
# frontend's extraction flow already consumes.
class OrderLineData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    position_description: str = Field(default="", alias="positionDescription")
    unit_price: float = Field(default=0, alias="unitPrice")
    amount: float = 0
    unit: str = ""
    total_price: float = Field(default=0, alias="totalPrice")
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern="^[A-Za-z]{3}$")

    @model_validator(mode="after")
    def normalize_currency(self) -> "OrderLineData":
        self.currency = self.currency.upper()
        return self

    @model_validator(mode="before")
    @classmethod
    def normalize_missing_values(cls, value: object) -> object:
        """AI providers commonly use null for information not found in a PDF.

        A missing value should result in a reviewable, partial extraction, not
        abort the entire request because the API schema expects a string/number.
        """
        if not isinstance(value, dict):
            return value
        cleaned = dict(value)
        for field, snake in (
            ("positionDescription", "position_description"),
            ("unit", "unit"),
        ):
            key = field if field in cleaned else snake
            if cleaned.get(key) is None:
                cleaned[key] = ""
        for field, snake in (
            ("unitPrice", "unit_price"),
            ("amount", "amount"),
            ("totalPrice", "total_price"),
        ):
            key = field if field in cleaned else snake
            cleaned[key] = _empty_numeric(cleaned.get(key))
        if not cleaned.get("currency"):
            cleaned["currency"] = "USD"
        return cleaned


class AdjustmentData(BaseModel):
    """A tax or discount extracted from a quote.

    Amounts are always positive. ``kind`` determines whether the amount is
    added to or subtracted from the selected item subtotal. ``lineIndex`` is
    zero-based and identifies the affected order line for line-level entries.
    """

    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["tax", "discount", "charge"]
    description: str = ""
    amount: float = Field(ge=0)
    scope: Literal["line", "order"] = "order"
    line_index: int | None = Field(default=None, alias="lineIndex")

    @model_validator(mode="before")
    @classmethod
    def normalize_optional_adjustment_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        cleaned = dict(value)
        amount_key = "amount" if "amount" in cleaned else "amount"
        cleaned[amount_key] = _empty_numeric(cleaned.get(amount_key))
        index_key = "lineIndex" if "lineIndex" in cleaned else "line_index"
        line_index = cleaned.get(index_key)
        if isinstance(line_index, str) and not line_index.strip():
            cleaned[index_key] = None
        if cleaned.get("description") is None:
            cleaned["description"] = ""
        return cleaned


class ExtractedVendorData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str = ""
    vendor_name: str = Field(default="", alias="vendorName")
    vat_id: str = Field(default="", alias="vatId")
    requestor_name: str = Field(default="", alias="requestorName")
    department: str = ""
    order_lines: list[OrderLineData] = Field(default_factory=list, alias="orderLines")
    total_cost: float = Field(default=0, alias="totalCost")
    adjustments: list[AdjustmentData] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_missing_values(cls, value: object) -> object:
        """Accept a partial AI response and surface its missing fields to the UI."""
        if not isinstance(value, dict):
            return value
        cleaned = dict(value)
        for alias, name in (
            ("title", "title"),
            ("vendorName", "vendor_name"),
            ("vatId", "vat_id"),
            ("requestorName", "requestor_name"),
            ("department", "department"),
        ):
            key = alias if alias in cleaned else name
            if cleaned.get(key) is None:
                cleaned[key] = ""

        total_key = "totalCost" if "totalCost" in cleaned else "total_cost"
        cleaned[total_key] = _empty_numeric(cleaned.get(total_key))

        for alias, name in (("orderLines", "order_lines"), ("adjustments", "adjustments")):
            key = alias if alias in cleaned else name
            if not isinstance(cleaned.get(key), list):
                cleaned[key] = []

        # Ignore malformed individual rows so one bad optional/partial AI row
        # cannot discard otherwise usable quote data.
        lines_key = "orderLines" if "orderLines" in cleaned else "order_lines"
        cleaned[lines_key] = [
            line for line in cleaned[lines_key] if isinstance(line, dict)
        ]

        # Tax/discount rows are optional enrichment. One malformed row should
        # not invalidate otherwise useful vendor and item extraction.
        adjustments_key = "adjustments"
        normalized_adjustments = []
        if isinstance(cleaned.get(adjustments_key), list):
            for adjustment in cleaned[adjustments_key]:
                if not isinstance(adjustment, dict):
                    continue
                row = dict(adjustment)
                kind = row.get("kind")
                if kind not in {"tax", "discount", "charge"}:
                    continue
                scope = row.get("scope") or "order"
                if scope not in {"line", "order"}:
                    continue
                index_key = "lineIndex" if "lineIndex" in row else "line_index"
                index = row.get(index_key)
                if isinstance(index, str) and not index.strip():
                    index = None
                if scope == "line":
                    try:
                        index = int(index)
                    except (TypeError, ValueError):
                        index = None
                # An unlinked line adjustment cannot be safely assigned. Drop
                # that adjustment while preserving the rest of the extraction.
                if scope == "line" and (
                    index is None or index < 0 or index >= len(cleaned[lines_key])
                ):
                    continue
                amount = _empty_numeric(row.get("amount"))
                try:
                    if float(amount) < 0:
                        continue
                except (TypeError, ValueError):
                    continue
                if scope == "order":
                    index = None
                row["scope"] = scope
                row[index_key] = index
                row["amount"] = amount
                normalized_adjustments.append(row)
        cleaned[adjustments_key] = normalized_adjustments
        return cleaned


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    success: bool
    data: ExtractedVendorData | None = None
    error: str | None = None
    missing_fields: list[str] | None = Field(default=None, alias="missingFields")
