import json
import logging
import re

from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import CommodityGroup
from app.schemas.extraction import AdjustmentData, ExtractedVendorData, ExtractionResponse

logger = logging.getLogger(__name__)

_ALTERNATIVE_LINE = re.compile(r"^\s*(?:alternative|alt\.?|optional)\b", re.IGNORECASE)
_ADDITIONAL_CHARGE_LINE = re.compile(
    r"^\s*(?:shipping|freight|delivery|transport|handling)\b", re.IGNORECASE
)

SYSTEM_PROMPT = (
    "You are a data extraction and classification specialist. Always respond with "
    "valid JSON only, no markdown or code blocks. Extract all information accurately "
    "from US business documents and classify into the appropriate commodity "
    "group based on the full context of the offer."
)


def _build_prompt(pdf_text: str, commodity_groups_list: str) -> str:
    return f"""You are a data extraction and classification specialist. Extract procurement/vendor offer information from the following US document text AND classify it into the appropriate commodity group.

Document Text:
{pdf_text}

Please extract the following information:
1. Title/Short Description (A brief summary of what this procurement is for - e.g., "Office Equipment Purchase", "Software Licenses", etc.)
2. Vendor Name (Vendor/Supplier/Seller/Company name)
3. Tax ID (Employer Identification Number/EIN/Federal Tax ID/TIN/VAT ID)
4. Department/Customer (Department/Customer/Bill To/Customer name)
5. Order Lines/Items (All line items with):
   - Position Description (Description/Product/Item/Article)
   - Unit Price (Unit Price/Price/Rate/Price per unit)
   - Amount (Qty/Quantity/Count)
   - Unit (Unit/UoM - e.g., "licenses", "pieces", "ea.", "items")
   - Total Price: the exact final total printed for that selected item. Do not calculate it from unit price and quantity.
6. Adjustments. Extract taxes, discounts, and additional charges separately with a positive amount:
   - kind: "tax", "discount", or "charge" (use "charge" for shipping, delivery, freight, handling, or similar costs)
   - description: the label in the quote
   - scope: "line" for an individual item or "order" for the complete quote
   - lineIndex: zero-based integer index of the matching order line for line adjustments; JSON null for order-level adjustments
7. Total Cost (the stated Grand Total/Total Cost/Total Offer Cost after all adjustments)
8. Commodity Group Classification - Select the SINGLE most appropriate commodity group from the list below

Available Commodity Groups (ID | Category | Name):
{commodity_groups_list}

IMPORTANT INSTRUCTIONS:
- For the title, create a SHORT (2-5 words) description based on the main items being purchased
- Extract only the purchased products/services as order lines. NEVER extract alternatives, options, substitutes, shipping, delivery, freight, handling, or lines marked "Alternative", "Optional", "Alt.", or similar as order lines.
- For unit prices and totals, extract only the numeric value (remove currency symbols)
- For the unit field, write out abbreviations in plain English (e.g., "ea." -> "pieces", "lic." -> "licenses")
- Be careful with US number formatting (e.g., "1,438.00" = 1438.00)
- Copy printed amounts; never calculate taxes, discounts, charges, or line totals. Item-specific taxes/discounts are reference information only. The quote-level total check uses only sum(orderLines.totalPrice) + quote-level taxes/charges - quote-level discounts.
- Every JSON key must be present. For missing text fields, return an empty string (""); for missing numeric fields return 0; for missing lists return []. For an order-level adjustment, lineIndex MUST be JSON null (not an empty string). For an item-level adjustment, lineIndex MUST be an integer. Use null for commodityGroupId or commodityGroupName only when no category can be selected.
- For classification, consider the PRIMARY nature and purpose of the items:
  * Branded office decor/furniture → Office Equipment (15)
  * Traditional promotional items (brochures, giveaways, banners) → Promotional Materials (43)
  * Software/IT products → Software (31) or IT Services (30)
  * If genuinely uncertain, set commodityGroupId to null

Return ONLY valid JSON in this exact format (no markdown, no code blocks):
{{
  "title": "Short description of procurement",
  "vendorName": "Company Name",
  "vatId": "12-3456789",
  "department": "Department Name",
  "orderLines": [
    {{
      "positionDescription": "Item description",
      "unitPrice": 150.00,
      "amount": 10,
      "unit": "licenses",
      "totalPrice": 1500.00
    }}
  ],
  "totalCost": 1500.00,
  "adjustments": [
    {{
      "kind": "tax",
      "description": "Sales tax",
      "amount": 120.00,
      "scope": "order",
      "lineIndex": null
    }}
  ],
  "commodityGroupId": 15,
  "commodityGroupName": "Office Equipment"
}}"""


def _strip_markdown_fences(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned.removeprefix("```json").strip()
    elif cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").strip()
    if cleaned.endswith("```"):
        cleaned = cleaned.removesuffix("```").strip()
    return cleaned


def _exclude_alternative_lines(extracted: ExtractedVendorData) -> None:
    """Guard against an AI accidentally selecting a quote alternative.

    The prompt is the primary instruction, while this small deterministic pass
    handles the common explicit labels found in the supplied vendor quotes.
    Line adjustment indexes are remapped with the retained order lines.
    """
    kept_lines = []
    index_map: dict[int, int] = {}
    for old_index, line in enumerate(extracted.order_lines):
        if _ALTERNATIVE_LINE.search(line.position_description):
            logger.info("Excluded alternative line from extraction: %s", line.position_description)
            continue
        index_map[old_index] = len(kept_lines)
        kept_lines.append(line)

    if len(kept_lines) == len(extracted.order_lines):
        return

    extracted.order_lines = kept_lines
    retained_adjustments = []
    for adjustment in extracted.adjustments:
        if adjustment.scope != "line":
            retained_adjustments.append(adjustment)
        elif adjustment.line_index in index_map:
            adjustment.line_index = index_map[adjustment.line_index]
            retained_adjustments.append(adjustment)
    extracted.adjustments = retained_adjustments


def _move_additional_charge_lines(extracted: ExtractedVendorData) -> None:
    """Keep shipping-like charges out of the purchased-item list.

    The prompt asks the model for this shape. This fallback covers the common
    case where a quote renders freight or delivery in the same table as items.
    """
    kept_lines = []
    index_map: dict[int, int] = {}
    new_charges: list[AdjustmentData] = []
    for old_index, line in enumerate(extracted.order_lines):
        if _ADDITIONAL_CHARGE_LINE.search(line.position_description):
            new_charges.append(
                AdjustmentData(
                    kind="charge",
                    description=line.position_description,
                    amount=line.total_price,
                    scope="order",
                    line_index=None,
                )
            )
            continue
        index_map[old_index] = len(kept_lines)
        kept_lines.append(line)

    if not new_charges:
        return

    extracted.order_lines = kept_lines
    retained_adjustments = []
    for adjustment in extracted.adjustments:
        if adjustment.scope != "line":
            retained_adjustments.append(adjustment)
        elif adjustment.line_index in index_map:
            adjustment.line_index = index_map[adjustment.line_index]
            retained_adjustments.append(adjustment)

    # Avoid duplicating a charge the AI already returned as an adjustment.
    existing_charge_keys = {
        (item.description.strip().lower(), item.amount)
        for item in retained_adjustments
        if item.kind == "charge" and item.scope == "order"
    }
    retained_adjustments.extend(
        charge
        for charge in new_charges
        if (charge.description.strip().lower(), charge.amount) not in existing_charge_keys
    )
    extracted.adjustments = retained_adjustments


def extract_vendor_data(pdf_text: str, db: Session) -> ExtractionResponse:
    """Extract vendor data and classify commodity group in one AI call.

    Port of ``src/actions/extraction.ts`` — same prompt, model and validation.
    """
    if not pdf_text or not pdf_text.strip():
        return ExtractionResponse(
            success=False,
            error="This PDF does not contain readable text. Please enter the request details manually.",
        )

    if not settings.openai_api_key:
        return ExtractionResponse(
            success=False,
            error="PDF extraction is not available right now. Please enter the request details manually.",
        )

    commodity_groups = list(
        db.scalars(select(CommodityGroup).order_by(CommodityGroup.id))
    )
    if not commodity_groups:
        return ExtractionResponse(
            success=False,
            error="PDF extraction is not available right now. Please enter the request details manually.",
        )

    # Give the model the full commodity catalog so it has complete context to
    # pick the most accurate match in a single pass.
    commodity_groups_list = "\n".join(
        f"{g.id} | {g.category} | {g.name}" for g in commodity_groups
    )

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_prompt(pdf_text, commodity_groups_list)},
            ],
            max_completion_tokens=4000,
            temperature=0.3,
        )

        content = response.choices[0].message.content
        if not content:
            return ExtractionResponse(
                success=False,
                error="We could not extract this quote. Please enter the request details manually.",
            )

        extracted = ExtractedVendorData.model_validate(
            json.loads(_strip_markdown_fences(content))
        )
        _exclude_alternative_lines(extracted)
        _move_additional_charge_lines(extracted)
    except Exception:  # noqa: BLE001 — errors are logged but never exposed to requesters
        logger.exception("Error extracting vendor data")
        return ExtractionResponse(
            success=False,
            error=(
                "We could not extract this quote. Please review the document "
                "or enter its details manually."
            ),
        )

    # Check which fields are missing or empty (same rules as the original).
    missing_fields: list[str] = []
    if not extracted.title.strip():
        missing_fields.append("Title/Short Description")
    if not extracted.vendor_name.strip():
        missing_fields.append("Vendor Name")
    if not extracted.vat_id.strip():
        missing_fields.append("Tax ID")
    if not extracted.department.strip():
        missing_fields.append("Department")
    if not extracted.order_lines:
        missing_fields.append("Order Lines")
    if not extracted.total_cost:
        missing_fields.append("Total Cost")
    if not extracted.commodity_group_id or not extracted.commodity_group_name:
        missing_fields.append("Commodity Group")

    # Validate that the returned commodity group id actually exists.
    if extracted.commodity_group_id is not None:
        valid_ids = {g.id for g in commodity_groups}
        if extracted.commodity_group_id not in valid_ids:
            logger.warning(
                "Invalid commodity group ID returned: %s", extracted.commodity_group_id
            )
            extracted.commodity_group_id = None
            extracted.commodity_group_name = None
            if "Commodity Group" not in missing_fields:
                missing_fields.append("Commodity Group")

    return ExtractionResponse(
        success=True,
        data=extracted,
        missing_fields=missing_fields or None,
    )
