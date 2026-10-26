"""Focused tests for quote extraction's deterministic safeguards."""
from decimal import Decimal

from app.schemas.extraction import ExtractedVendorData
from app.services.extraction import _exclude_alternative_lines, _move_additional_charge_lines
from app.services.reconciliation import calculate_total


def test_partial_extraction_normalizes_null_text_fields() -> None:
    extracted = ExtractedVendorData.model_validate(
        {
            "title": "Grinding machine",
            "vendorName": "Hartwell Industrial Tools",
            "vatId": None,
            "department": None,
            "orderLines": [],
            "totalCost": 0,
            "adjustments": None,
            "commodityGroupId": None,
            "commodityGroupName": None,
        }
    )

    assert extracted.vat_id == ""
    assert extracted.department == ""
    assert extracted.adjustments == []


def test_total_reconciliation_uses_quote_line_totals_and_order_adjustments() -> None:
    extracted = ExtractedVendorData.model_validate(
        {
            "title": "Moss panels",
            "vendorName": "Greenmantle Moss Studio",
            "vatId": "",
            "department": "",
            # Alternatives are intentionally absent from selected order lines.
            "orderLines": [
                {
                    "positionDescription": "Moss Art Panel",
                    "unitPrice": 894.08,
                    "amount": 1,
                    "unit": "pieces",
                    "totalPrice": 715.26,
                },
                {
                    "positionDescription": "Logo integration",
                    "unitPrice": 622,
                    "amount": 1,
                    "unit": "pieces",
                    "totalPrice": 622,
                },
            ],
            "adjustments": [
                {
                    "kind": "discount",
                    "description": "20% panel discount",
                    "amount": 178.82,
                    "scope": "line",
                    "lineIndex": 0,
                },
                {
                    "kind": "charge",
                    "description": "Shipping",
                    "amount": 215,
                    "scope": "order",
                    "lineIndex": None,
                },
                {
                    "kind": "tax",
                    "description": "Sales tax on items",
                    "amount": 115.34,
                    "scope": "order",
                    "lineIndex": None,
                },
                {
                    "kind": "tax",
                    "description": "Sales tax on shipping",
                    "amount": 18.54,
                    "scope": "order",
                    "lineIndex": None,
                },
            ],
            "totalCost": 1686.14,
            "commodityGroupId": 15,
            "commodityGroupName": "Office Equipment",
        }
    )

    assert calculate_total(extracted.order_lines, extracted.adjustments) == Decimal("1686.14")


def test_line_level_adjustments_are_not_included_in_total_check() -> None:
    extracted = ExtractedVendorData.model_validate(
        {
            "title": "Example",
            "vendorName": "Vendor",
            "vatId": "",
            "department": "",
            "orderLines": [
                {"positionDescription": "Quoted item", "unitPrice": 100, "amount": 1, "unit": "piece", "totalPrice": 90},
            ],
            "adjustments": [
                {"kind": "discount", "description": "Already in item total", "amount": 10, "scope": "line", "lineIndex": 0},
                {"kind": "tax", "description": "Order tax", "amount": 5, "scope": "order", "lineIndex": None},
            ],
            "totalCost": 95,
            "commodityGroupId": 15,
            "commodityGroupName": "Office Equipment",
        }
    )

    assert calculate_total(extracted.order_lines, extracted.adjustments) == Decimal("95.00")


def test_shipping_line_becomes_a_quote_level_additional_charge() -> None:
    extracted = ExtractedVendorData.model_validate(
        {
            "title": "Equipment",
            "vendorName": "Vendor",
            "vatId": "",
            "department": "",
            "orderLines": [
                {"positionDescription": "Grinding machine", "unitPrice": 15000, "amount": 1, "unit": "piece", "totalPrice": 15000},
                {"positionDescription": "Delivery and installation", "unitPrice": 2000, "amount": 1, "unit": "service", "totalPrice": 2000},
            ],
            "adjustments": [],
            "totalCost": 17000,
            "commodityGroupId": 45,
            "commodityGroupName": "Production Machinery",
        }
    )

    _move_additional_charge_lines(extracted)

    assert [line.position_description for line in extracted.order_lines] == ["Grinding machine"]
    assert extracted.adjustments[0].kind == "charge"
    assert extracted.adjustments[0].amount == 2000


def test_explicit_alternative_lines_and_their_adjustments_are_excluded() -> None:
    extracted = ExtractedVendorData.model_validate(
        {
            "title": "Panels",
            "vendorName": "Vendor",
            "vatId": "",
            "department": "",
            "orderLines": [
                {"positionDescription": "Selected panel", "unitPrice": 100, "amount": 1, "unit": "piece", "totalPrice": 100},
                {"positionDescription": "Alternative: larger panel", "unitPrice": 200, "amount": 1, "unit": "piece", "totalPrice": 200},
                {"positionDescription": "Installation", "unitPrice": 20, "amount": 1, "unit": "service", "totalPrice": 20},
            ],
            "adjustments": [
                {"kind": "tax", "description": "Alternative tax", "amount": 20, "scope": "line", "lineIndex": 1},
                {"kind": "tax", "description": "Installation tax", "amount": 2, "scope": "line", "lineIndex": 2},
            ],
            "totalCost": 122,
            "commodityGroupId": 15,
            "commodityGroupName": "Office Equipment",
        }
    )

    _exclude_alternative_lines(extracted)

    assert [line.position_description for line in extracted.order_lines] == ["Selected panel", "Installation"]
    assert len(extracted.adjustments) == 1
    assert extracted.adjustments[0].line_index == 1
