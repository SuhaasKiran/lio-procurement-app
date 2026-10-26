"""Edge cases for partial and unreliable AI extraction responses."""
from types import SimpleNamespace

import pytest

from app.schemas.extraction import ExtractedVendorData
from app.services import extraction


def test_null_and_missing_fields_produce_reviewable_partial_data() -> None:
    extracted = ExtractedVendorData.model_validate(
        {
            "vendorName": None,
            "vatId": None,
            "department": None,
            "orderLines": [
                {"positionDescription": None, "unitPrice": None, "amount": "", "totalPrice": None},
                "not an object",
            ],
            "totalCost": None,
            "adjustments": None,
        }
    )
    assert extracted.vendor_name == extracted.vat_id == extracted.department == ""
    assert extracted.total_cost == 0
    assert len(extracted.order_lines) == 1
    assert extracted.order_lines[0].position_description == ""
    assert extracted.order_lines[0].total_price == 0
    assert extracted.adjustments == []


def test_malformed_or_unlinked_optional_adjustments_are_dropped() -> None:
    extracted = ExtractedVendorData.model_validate(
        {
            "orderLines": [{"positionDescription": "Item", "totalPrice": 10}],
            "adjustments": [
                None,
                {"kind": "other", "amount": 1},
                {"kind": "tax", "amount": 1, "scope": "unknown"},
                {"kind": "tax", "amount": -1, "scope": "order"},
                {"kind": "tax", "amount": 2, "scope": "line", "lineIndex": 8},
                {"kind": "discount", "amount": 3, "scope": "line", "lineIndex": 0},
            ],
        }
    )
    assert [(item.kind, item.amount, item.line_index) for item in extracted.adjustments] == [
        ("discount", 3, 0)
    ]


def _mock_openai(monkeypatch, content):
    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **_params: SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
                    )
                )
            )

    monkeypatch.setattr(extraction, "OpenAI", FakeClient)
    monkeypatch.setattr(extraction.settings, "openai_api_key", "test-key")


def test_extraction_returns_success_with_missing_required_fields(monkeypatch) -> None:
    _mock_openai(
        monkeypatch,
        '{"title":"Tools", "vendorName":"Hartwell", "vatId":null, '
        '"department":"", "orderLines":[{"positionDescription":"Machine", '
        '"unitPrice":15000,"amount":1,"unit":"piece","totalPrice":15000}], '
        '"totalCost":18402.5}',
    )

    response = extraction.extract_vendor_data("quote text", db=None)

    assert response.success is True
    assert response.error is None
    assert response.data is not None and response.data.vat_id == ""
    assert response.missing_fields == ["Tax ID", "Department"]


@pytest.mark.parametrize("content", ["not JSON", "{} trailing"])
def test_invalid_model_json_returns_safe_failure(monkeypatch, content) -> None:
    _mock_openai(monkeypatch, content)
    response = extraction.extract_vendor_data("quote text", db=None)
    assert response.success is False
    assert response.data is None
    assert response.error == (
        "We could not extract this quote. Please review the document "
        "or enter its details manually."
    )


@pytest.mark.parametrize("content", [None, ""])
def test_empty_model_response_returns_manual_entry_message(monkeypatch, content) -> None:
    _mock_openai(monkeypatch, content)
    response = extraction.extract_vendor_data("quote text", db=None)
    assert response.success is False
    assert response.data is None
    assert response.error == "We could not extract this quote. Please enter the request details manually."


def test_empty_pdf_text_and_missing_api_key_fail_before_model_call(monkeypatch) -> None:
    monkeypatch.setattr(extraction.settings, "openai_api_key", "")
    no_text = extraction.extract_vendor_data("  ", db=None)
    no_key = extraction.extract_vendor_data("some text", db=None)
    assert no_text.success is False
    assert "readable text" in no_text.error
    assert no_key.success is False
    assert "not available" in no_key.error
