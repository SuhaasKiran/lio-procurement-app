"""Focused tests for catalog suggestions and supplier-split request creation."""
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import (
    Article,
    CommodityGroup,
    Organization,
    OrganizationMember,
    ProcurementRequest,
    Supplier,
    User,
)
from app.routers.requests import create_request_batch
from app.schemas.requests import RequestCreate
from app.services.article_matching import suggest_article_page, suggest_articles


@pytest.fixture
def catalog_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        org = Organization(name="Test Org", slug=f"test-{uuid.uuid4()}")
        other_org = Organization(name="Other Org", slug=f"other-{uuid.uuid4()}")
        user = User(email=f"{uuid.uuid4()}@example.com", hashed_password="x")
        db.add_all([org, other_org, user, CommodityGroup(id=31, category="IT", name="Software")])
        db.flush()
        membership = OrganizationMember(
            organization_id=org.id, user_id=user.id, role="requester"
        )
        original_supplier = Supplier(organization_id=org.id, name="Quote Vendor", vat_id="Q-1")
        catalog_supplier = Supplier(organization_id=org.id, name="Catalog Vendor", vat_id="C-1")
        foreign_supplier = Supplier(organization_id=other_org.id, name="Foreign Vendor")
        db.add_all([membership, original_supplier, catalog_supplier, foreign_supplier])
        db.flush()
        article = Article(
            organization_id=org.id,
            article_number="LT-14",
            supplier_id=catalog_supplier.id,
            description='Business laptop 14 inch, 16 GB RAM',
            unit_price=1000,
            currency="USD",
            unit="piece",
            quantity=1,
        )
        foreign_article = Article(
            organization_id=other_org.id,
            article_number="LT-14",
            supplier_id=foreign_supplier.id,
            description='Business laptop 14 inch, 16 GB RAM',
            unit_price=1,
            currency="USD",
            unit="piece",
            quantity=1,
        )
        dock = Article(
            organization_id=org.id,
            article_number="DK-2",
            supplier_id=catalog_supplier.id,
            description="USB-C laptop docking station",
            unit_price=200,
            currency="USD",
            unit="piece",
            quantity=1,
        )
        db.add_all([article, foreign_article, dock])
        db.commit()
        yield db, membership, article, foreign_article
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_suggestions_rank_exact_article_and_match_keywords_within_org(catalog_db):
    db, membership, article, _foreign = catalog_db

    exact = suggest_articles(db, membership, "lt-14")
    assert exact[0][0].id == article.id
    assert exact[0][1] == "article_number"

    keyword_matches = suggest_articles(db, membership, "14-inch business laptop")
    assert keyword_matches
    assert keyword_matches[0][0].id == article.id
    assert keyword_matches[0][1] == "keywords"

    business_laptop_matches = suggest_articles(db, membership, "business laptop")
    assert business_laptop_matches[0][0].id == article.id
    assert business_laptop_matches[0][3] == ["business", "laptop"]

    alias_matches = suggest_articles(db, membership, "docking station")
    assert alias_matches[0][0].article_number == "DK-2"


def test_suggestions_do_not_return_another_organizations_articles(catalog_db):
    db, membership, article, foreign_article = catalog_db

    matches = suggest_articles(db, membership, "LT-14")

    assert [match[0].id for match in matches] == [article.id]
    assert foreign_article.id not in [match[0].id for match in matches]


def test_suggestions_ignore_queries_without_searchable_terms(catalog_db):
    db, membership, _article, _foreign = catalog_db
    assert suggest_articles(db, membership, "the and of") == []


def test_suggestion_pages_report_total_and_do_not_repeat_items(catalog_db):
    db, membership, _article, _foreign = catalog_db

    first_page, total = suggest_article_page(db, membership, "laptop", limit=1)
    second_page, repeated_total = suggest_article_page(db, membership, "laptop", limit=1, offset=1)

    assert total == repeated_total
    assert total >= 2
    assert first_page[0][0].id != second_page[0][0].id


def _payload(lines, **overrides):
    values = {
        "requestorName": "Taylor Requester",
        "titleShortDescription": "Equipment",
        "vendorName": "Quote Vendor",
        "vatId": "Q-1",
        "commodityGroupId": 31,
        "totalCost": 1200,
        "sourceQuoteCurrency": "USD",
        "orderLines": lines,
    }
    values.update(overrides)
    return RequestCreate.model_validate(values)


def _line(description, price, quantity, *, article_id=None, currency="USD", unit="piece"):
    return {
        "positionDescription": description,
        "commodityGroupId": 31,
        "classificationSource": "manual",
        "unitPrice": price,
        "amount": quantity,
        "unit": unit,
        "totalPrice": price * quantity,
        "currency": currency,
        "catalogArticleId": str(article_id) if article_id else None,
    }


def test_batch_keeps_each_request_to_one_supplier_and_uses_catalog_snapshot(catalog_db):
    db, membership, article, _foreign = catalog_db
    payload = _payload(
        [
            _line("Quote-only cable", 20, 2),
            _line(article.description, 1000, 1, article_id=article.id),
        ]
    )

    created = create_request_batch(payload, membership, db)

    assert len(created) == 2
    by_vendor = {request.vendor_name: request for request in created}
    assert set(by_vendor) == {"Quote Vendor", "Catalog Vendor"}
    assert by_vendor["Quote Vendor"].supplier_id is None
    assert by_vendor["Quote Vendor"].total_cost == 40
    catalog_request = by_vendor["Catalog Vendor"]
    assert catalog_request.supplier_id == article.supplier_id
    assert catalog_request.total_cost == 1000
    assert catalog_request.order_lines[0].catalog_article_id == article.id
    assert catalog_request.order_lines[0].catalog_article_number == "LT-14"


def test_batch_rejects_catalog_item_from_another_organization_without_writes(catalog_db):
    db, membership, _article, foreign_article = catalog_db
    payload = _payload([_line(foreign_article.description, 1, 1, article_id=foreign_article.id)])

    with pytest.raises(HTTPException) as error:
        create_request_batch(payload, membership, db)

    assert error.value.status_code == 422
    assert db.scalar(select(func.count()).select_from(ProcurementRequest)) == 0


def test_batch_rejects_stale_catalog_price_without_writes(catalog_db):
    db, membership, article, _foreign = catalog_db
    payload = _payload([_line(article.description, 999, 1, article_id=article.id)])

    with pytest.raises(HTTPException) as error:
        create_request_batch(payload, membership, db)

    assert error.value.status_code == 409
    assert db.scalar(select(func.count()).select_from(ProcurementRequest)) == 0


def test_order_level_quote_adjustment_requires_original_supplier_group(catalog_db):
    db, membership, article, _foreign = catalog_db
    payload = _payload(
        [_line(article.description, 1000, 1, article_id=article.id)],
        financialBreakdown={
            "adjustments": [{"kind": "tax", "description": "Tax", "amount": 50, "scope": "order"}]
        },
    )

    with pytest.raises(HTTPException) as error:
        create_request_batch(payload, membership, db)

    assert error.value.status_code == 422
    assert db.scalar(select(func.count()).select_from(ProcurementRequest)) == 0
