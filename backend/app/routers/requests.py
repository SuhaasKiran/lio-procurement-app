import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.deps import get_current_membership, require_buyer_or_admin
from app.db.session import get_db
from app.models import (
    Article,
    CommodityGroup,
    OrderLine,
    Organization,
    OrganizationMember,
    ProcurementRequest,
    RequestActivity,
    RequestDocument,
)
from app.services.reconciliation import (
    calculate_total,
    money,
    validate_adjustment_line_indexes,
)
from app.schemas.requests import (
    ApprovalDecision,
    FinancialBreakdownIn,
    OrderLineIn,
    RequestActivityOut,
    RequestCreate,
    RequestOut,
    RequestUpdate,
    StatusUpdate,
)

router = APIRouter(prefix="/requests", tags=["requests"])

MAX_PDF_BYTES = 10 * 1024 * 1024

# Human-readable labels for the fields we track in the activity log.
_FIELD_LABELS = {
    "requestor_name": "requestor",
    "title": "title",
    "vendor_name": "vendor",
    "currency": "currency",
    "vat_id": "tax ID",
    "commodity_group_id": "commodity group",
    "total_cost": "total cost",
    "department": "department",
    "order_lines": "order lines",
    "financial_breakdown": "taxes and discounts",
    "discrepancy_note": "discrepancy note",
}


def _load_request_options():
    return (
        selectinload(ProcurementRequest.order_lines),
        selectinload(ProcurementRequest.commodity_group),
    )


def _get_org_request(
    db: Session, request_id: uuid.UUID, organization_id: uuid.UUID
) -> ProcurementRequest:
    request = db.scalar(
        select(ProcurementRequest)
        .options(*_load_request_options())
        # Refresh already-loaded relationships (e.g. after an update commit).
        .execution_options(populate_existing=True)
        .where(
            ProcurementRequest.id == request_id,
            ProcurementRequest.organization_id == organization_id,
        )
    )
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Request not found or you do not have permission to access it",
        )
    return request


def _validate_commodity_group(db: Session, commodity_group_id: int) -> None:
    if db.get(CommodityGroup, commodity_group_id) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown commodity group id: {commodity_group_id}",
        )


def _enforce_required_fields(
    db: Session, organization_id: uuid.UUID, payload: RequestCreate | RequestUpdate
) -> None:
    """Enforce the org's configurable required fields (see OrganizationSettings)."""
    org = db.get(Organization, organization_id)
    required = (org.settings or {}).get("required_fields", []) if org else []
    missing = [field for field in required if not getattr(payload, field, None)]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Missing fields required by your organization: {', '.join(missing)}",
        )


def _validate_request_financials(
    payload: RequestCreate | RequestUpdate,
) -> tuple[dict, str | None]:
    """Calculate quote totals server-side and enforce discrepancy acknowledgement.

    The browser displays the same calculation, but this check is deliberately
    repeated here so direct API clients cannot submit an unexplained mismatch.
    """
    adjustments = payload.financial_breakdown.adjustments
    try:
        validate_adjustment_line_indexes(adjustments, len(payload.order_lines))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    calculated_total = calculate_total(payload.order_lines, adjustments)
    quoted_total = money(payload.total_cost)
    mismatch = calculated_total != quoted_total
    note = (payload.discrepancy_note or "").strip() or None
    if mismatch and not note:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "The quoted total does not match the selected items, taxes, and "
                "discounts. Correct the quoted total or add a discrepancy note."
            ),
        )

    breakdown = {
        "adjustments": [adjustment.model_dump(by_alias=True) for adjustment in adjustments],
        "calculatedTotal": float(calculated_total),
        "quotedTotal": float(quoted_total),
        "hasMismatch": mismatch,
    }
    return breakdown, note


def _log_activity(
    db: Session,
    request_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    summary: str,
    detail: dict | None = None,
) -> None:
    db.add(
        RequestActivity(
            request_id=request_id,
            actor_id=actor_id,
            action=action,
            summary=summary,
            detail=detail,
        )
    )


def _order_lines_signature(lines) -> list[tuple]:
    return [
        (
            line.position_description,
            float(line.unit_price),
            float(line.amount),
            line.unit,
            float(line.total_price),
            getattr(line, "catalog_article_id", None),
            getattr(line, "catalog_article_number", None),
        )
        for line in lines
    ]


def _changed_fields(request: ProcurementRequest, payload: RequestUpdate) -> list[str]:
    """Return the field keys whose value differs between the request and payload."""
    changed: list[str] = []
    if request.requestor_name != payload.requestor_name:
        changed.append("requestor_name")
    if request.title != payload.title:
        changed.append("title")
    if request.vendor_name != payload.vendor_name:
        changed.append("vendor_name")
    if payload.order_lines and request.currency != payload.order_lines[0].currency:
        changed.append("currency")
    if (request.vat_id or "") != (payload.vat_id or ""):
        changed.append("vat_id")
    if request.commodity_group_id != payload.commodity_group_id:
        changed.append("commodity_group_id")
    if float(request.total_cost or 0) != float(payload.total_cost or 0):
        changed.append("total_cost")
    if (request.department or "") != (payload.department or ""):
        changed.append("department")
    old_lines = _order_lines_signature(
        sorted(request.order_lines, key=lambda line: line.line_order)
    )
    new_lines = _order_lines_signature(payload.order_lines)
    if old_lines != new_lines:
        changed.append("order_lines")
    new_breakdown, new_note = _validate_request_financials(payload)
    if (request.financial_breakdown or {}) != new_breakdown:
        changed.append("financial_breakdown")
    if (request.discrepancy_note or None) != new_note:
        changed.append("discrepancy_note")
    return changed


@router.get("", response_model=list[RequestOut])
def list_requests(
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> list[RequestOut]:
    requests = db.scalars(
        select(ProcurementRequest)
        .options(*_load_request_options())
        .where(ProcurementRequest.organization_id == membership.organization_id)
        .order_by(ProcurementRequest.created_at.desc())
    )
    return [RequestOut.model_validate(r) for r in requests]


def _normalized_supplier_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _single_request_currency(lines: list[OrderLineIn]) -> str:
    currencies = {line.currency for line in lines}
    if len(currencies) > 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A request can contain items in only one currency. Split the items by currency.",
        )
    return next(iter(currencies), "USD")


def _make_request_lines(lines: list[OrderLineIn]) -> list[OrderLine]:
    return [
        OrderLine(
            position_description=line.position_description,
            catalog_article_id=line.catalog_article_id,
            catalog_article_number=line.catalog_article_number,
            unit_price=line.unit_price,
            amount=line.amount,
            unit=line.unit,
            total_price=line.total_price,
            line_order=index + 1,
        )
        for index, line in enumerate(lines)
    ]


@router.post("/batch", response_model=list[RequestOut], status_code=status.HTTP_201_CREATED)
def create_request_batch(
    payload: RequestCreate,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> list[RequestOut]:
    """Create one request per selected supplier, in a single transaction."""
    _validate_commodity_group(db, payload.commodity_group_id)
    if any(line.catalog_article_id is None for line in payload.order_lines) and not payload.vendor_name.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Vendor name is required for items that remain from the original quote.",
        )

    selected_ids = {
        line.catalog_article_id
        for line in payload.order_lines
        if line.catalog_article_id is not None
    }
    articles = list(
        db.scalars(
            select(Article)
            .where(
                Article.id.in_(selected_ids),
                Article.organization_id == membership.organization_id,
            )
            .options(joinedload(Article.supplier))
        )
    ) if selected_ids else []
    articles_by_id = {article.id: article for article in articles}
    if len(articles_by_id) != len(selected_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A selected catalog item is unavailable in your organization. Refresh the suggestions and try again.",
        )

    selected_supplier_ids_by_name: dict[str, set[uuid.UUID]] = {}
    for article in articles:
        if article.supplier:
            name_key = _normalized_supplier_name(article.supplier.name)
            selected_supplier_ids_by_name.setdefault(name_key, set()).add(article.supplier_id)

    groups: dict[tuple[str, str, str], dict] = {}
    line_group_keys: dict[int, tuple[str, str, str]] = {}
    any_catalog_selection = bool(selected_ids)
    original_name_key = _normalized_supplier_name(payload.vendor_name)

    for index, line in enumerate(payload.order_lines):
        article = articles_by_id.get(line.catalog_article_id) if line.catalog_article_id else None
        if article is not None:
            supplier_name = article.supplier.name if article.supplier else ""
            if not supplier_name:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="The selected catalog item has no active supplier.",
                )
            if (
                money(line.unit_price) != money(article.unit_price)
                or line.currency != article.currency.upper()
                or line.unit != article.unit
                or line.position_description != article.description
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This catalog item changed after it was selected. Search again and review the latest price before creating the request.",
                )
            currency = article.currency.upper()
            key = ("supplier", str(article.supplier_id), currency)
            line_data = OrderLineIn(
                positionDescription=article.description,
                unitPrice=float(article.unit_price),
                amount=line.amount,
                unit=article.unit,
                totalPrice=float(money(Decimal(str(article.unit_price)) * Decimal(str(line.amount)))),
                currency=currency,
                catalogArticleId=article.id,
                catalogArticleNumber=article.article_number,
            )
            supplier_id = article.supplier_id
            vendor_name = supplier_name
            vat_id = article.supplier.vat_id if article.supplier else None
        else:
            currency = line.currency
            original_key = _normalized_supplier_name(payload.vendor_name)
            matching_supplier_ids = selected_supplier_ids_by_name.get(original_key, set())
            if len(matching_supplier_ids) == 1:
                supplier_id = next(iter(matching_supplier_ids))
                key = ("supplier", str(supplier_id), currency)
                supplier_name = next(
                    article.supplier.name
                    for article in articles
                    if article.supplier_id == supplier_id and article.supplier
                )
                vendor_name = supplier_name
                selected_supplier = next(
                    article.supplier
                    for article in articles
                    if article.supplier_id == supplier_id and article.supplier
                )
                vat_id = selected_supplier.vat_id
            else:
                key = ("original", original_key, currency)
                supplier_id = None
                vendor_name = payload.vendor_name.strip()
                vat_id = payload.vat_id
            line_data = line.model_copy(
                update={"catalog_article_id": None, "catalog_article_number": None}
            )

        group = groups.setdefault(
            key,
            {
                "supplier_id": supplier_id,
                "vendor_name": vendor_name,
                "vat_id": vat_id,
                "currency": currency,
                "lines": [],
                "source_indexes": [],
                "adjustments": [],
            },
        )
        group["lines"].append(line_data)
        group["source_indexes"].append(index)
        line_group_keys[index] = key

    # Item-specific adjustments follow their item. Quote-level adjustments stay
    # with the original supplier, if that supplier appears in exactly one group.
    order_adjustments = [
        adjustment
        for adjustment in payload.financial_breakdown.adjustments
        if adjustment.scope == "order"
    ]
    for adjustment in payload.financial_breakdown.adjustments:
        if adjustment.scope != "line":
            continue
        source_index = adjustment.line_index
        if source_index is None or source_index not in line_group_keys:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A line adjustment refers to an item that is no longer in the request.",
            )
        group = groups[line_group_keys[source_index]]
        group_index = group["source_indexes"].index(source_index)
        group["adjustments"].append(
            adjustment.model_copy(update={"line_index": group_index})
        )

    if order_adjustments:
        original_supplier_groups = [
            (key, group)
            for key, group in groups.items()
            if _normalized_supplier_name(group["vendor_name"]) == original_name_key
        ]
        if len(original_supplier_groups) != 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Quote-level taxes, discounts, or charges must stay with the original supplier. "
                    "Keep an item with that supplier, in one currency, or remove the quote-level adjustments."
                ),
            )
        source_currency = payload.source_quote_currency or (
            payload.order_lines[0].currency if payload.order_lines else "USD"
        )
        if original_supplier_groups[0][1]["currency"] != source_currency:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "The quote-level adjustments use a different currency from the original-supplier request. "
                    "Correct the currencies or remove those adjustments before creating the requests."
                ),
            )
        original_supplier_groups[0][1]["adjustments"].extend(order_adjustments)

    if not groups:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Add at least one order line before creating a request.",
        )

    # Preserve the existing quote reconciliation behavior for an unchanged,
    # single-supplier request. Changed/split requests use their own line totals.
    preserve_quote_total = len(groups) == 1 and not any_catalog_selection
    requests_to_create: list[ProcurementRequest] = []
    for group in groups.values():
        group_payload = payload.model_copy(
            update={
                "vendor_name": group["vendor_name"],
                "vat_id": group["vat_id"],
                "order_lines": group["lines"],
                "financial_breakdown": FinancialBreakdownIn(
                    adjustments=group["adjustments"]
                ),
                "total_cost": payload.total_cost if preserve_quote_total else float(
                    calculate_total(group["lines"], group["adjustments"])
                ),
            }
        )
        _enforce_required_fields(db, membership.organization_id, group_payload)
        financial_breakdown, discrepancy_note = _validate_request_financials(group_payload)
        if not preserve_quote_total:
            financial_breakdown["sourceQuotedTotal"] = float(payload.total_cost)
            financial_breakdown["sourceQuotedCurrency"] = (
                payload.source_quote_currency
                or (payload.order_lines[0].currency if payload.order_lines else "USD")
            )
            financial_breakdown["splitSupplierCount"] = len(groups)

        request = ProcurementRequest(
            user_id=membership.user_id,
            requestor_name=payload.requestor_name,
            title=payload.title,
            vendor_name=group["vendor_name"],
            supplier_id=group["supplier_id"],
            currency=group["currency"],
            vat_id=group["vat_id"],
            commodity_group_id=payload.commodity_group_id,
            total_cost=group_payload.total_cost,
            financial_breakdown=financial_breakdown,
            discrepancy_note=discrepancy_note,
            department=payload.department,
            organization_id=membership.organization_id,
            created_by=membership.user_id,
            status="open",
        )
        request.order_lines = _make_request_lines(group["lines"])
        requests_to_create.append(request)

    try:
        db.add_all(requests_to_create)
        db.flush()
        for request in requests_to_create:
            _log_activity(
                db,
                request.id,
                membership.user_id,
                "created",
                "Request created" if len(requests_to_create) == 1 else "Request created from supplier split",
                {"supplier_split_count": len(requests_to_create)}
                if len(requests_to_create) > 1
                else None,
            )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return [
        RequestOut.model_validate(
            _get_org_request(db, request.id, membership.organization_id)
        )
        for request in requests_to_create
    ]


@router.get("/{request_id}", response_model=RequestOut)
def get_request(
    request_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> RequestOut:
    return RequestOut.model_validate(
        _get_org_request(db, request_id, membership.organization_id)
    )


@router.post("", response_model=RequestOut, status_code=status.HTTP_201_CREATED)
def create_request(
    payload: RequestCreate,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> RequestOut:
    if not payload.vendor_name.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Vendor name is required.",
        )
    if any(line.catalog_article_id for line in payload.order_lines):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Create catalog-based requests through the supplier-splitting endpoint.",
        )
    currency = _single_request_currency(payload.order_lines)
    _validate_commodity_group(db, payload.commodity_group_id)
    _enforce_required_fields(db, membership.organization_id, payload)
    financial_breakdown, discrepancy_note = _validate_request_financials(payload)

    # Vendor fields are already cleaned up during extraction, so we can persist
    # them directly here.
    request = ProcurementRequest(
        user_id=membership.user_id,
        requestor_name=payload.requestor_name,
        title=payload.title,
        vendor_name=payload.vendor_name,
        currency=currency,
        vat_id=payload.vat_id,
        commodity_group_id=payload.commodity_group_id,
        total_cost=payload.total_cost,
        financial_breakdown=financial_breakdown,
        discrepancy_note=discrepancy_note,
        department=payload.department,
        organization_id=membership.organization_id,
        created_by=membership.user_id,
        status="open",
    )
    request.order_lines = _make_request_lines(payload.order_lines)
    db.add(request)
    db.flush()
    _log_activity(db, request.id, membership.user_id, "created", "Request created")
    db.commit()

    request = _get_org_request(db, request.id, membership.organization_id)
    return RequestOut.model_validate(request)


@router.put("/{request_id}", response_model=RequestOut)
def update_request(
    request_id: uuid.UUID,
    payload: RequestUpdate,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> RequestOut:
    request = _get_org_request(db, request_id, membership.organization_id)
    currency = _single_request_currency(payload.order_lines)
    _validate_commodity_group(db, payload.commodity_group_id)
    _enforce_required_fields(db, membership.organization_id, payload)
    financial_breakdown, discrepancy_note = _validate_request_financials(payload)

    changed = _changed_fields(request, payload)

    requested_catalog_ids = {
        line.catalog_article_id
        for line in payload.order_lines
        if line.catalog_article_id is not None
    }
    catalog_articles = list(
        db.scalars(
            select(Article)
            .where(
                Article.id.in_(requested_catalog_ids),
                Article.organization_id == membership.organization_id,
            )
            .options(joinedload(Article.supplier))
        )
    ) if requested_catalog_ids else []
    articles_by_id = {article.id: article for article in catalog_articles}
    if len(articles_by_id) != len(requested_catalog_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A linked catalog item is unavailable in your organization.",
        )
    linked_supplier_ids = {article.supplier_id for article in catalog_articles}
    if len(linked_supplier_ids) > 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A request can contain catalog items from only one supplier.",
        )
    if request.supplier_id and linked_supplier_ids and linked_supplier_ids != {request.supplier_id}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A catalog-linked request cannot be changed to a different supplier.",
        )
    if request.supplier_id and payload.vendor_name.strip() != request.vendor_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A catalog-linked request must keep its selected supplier.",
        )

    if catalog_articles:
        selected_supplier = catalog_articles[0].supplier
        if (
            selected_supplier is None
            or _normalized_supplier_name(payload.vendor_name)
            != _normalized_supplier_name(selected_supplier.name)
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="The vendor name must match the selected catalog supplier.",
            )

    request.requestor_name = payload.requestor_name
    request.title = payload.title
    request.vendor_name = payload.vendor_name
    request.supplier_id = next(iter(linked_supplier_ids), request.supplier_id)
    request.currency = currency
    request.vat_id = payload.vat_id
    request.commodity_group_id = payload.commodity_group_id
    request.total_cost = payload.total_cost
    request.department = payload.department
    request.financial_breakdown = financial_breakdown
    request.discrepancy_note = discrepancy_note

    # Replace order lines (same semantics as the original delete-and-insert).
    request.order_lines = _make_request_lines(payload.order_lines)

    if changed:
        labels = [_FIELD_LABELS.get(f, f) for f in changed]
        _log_activity(
            db,
            request.id,
            membership.user_id,
            "updated",
            f"Updated {', '.join(labels)}",
            {"changed_fields": changed},
        )
    db.commit()

    request = _get_org_request(db, request_id, membership.organization_id)
    return RequestOut.model_validate(request)


@router.patch("/{request_id}/status", response_model=RequestOut)
def update_status(
    request_id: uuid.UUID,
    payload: StatusUpdate,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> RequestOut:
    request = _get_org_request(db, request_id, membership.organization_id)

    if request.status != payload.status:
        _log_activity(
            db,
            request.id,
            membership.user_id,
            "status_changed",
            f"Status changed from {request.status} to {payload.status}",
            {"from": request.status, "to": payload.status},
        )
        request.status = payload.status
        db.commit()

    request = _get_org_request(db, request_id, membership.organization_id)
    return RequestOut.model_validate(request)


@router.patch("/{request_id}/approval", response_model=RequestOut)
def decide_approval(
    request_id: uuid.UUID,
    payload: ApprovalDecision,
    membership: OrganizationMember = Depends(require_buyer_or_admin),
    db: Session = Depends(get_db),
) -> RequestOut:
    """Approve or reject a pending request (buyers and admins only)."""
    request = _get_org_request(db, request_id, membership.organization_id)

    if request.approval_status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Request has already been {request.approval_status}.",
        )

    request.approval_status = payload.decision
    request.approved_by = membership.user_id
    request.approved_at = datetime.now(timezone.utc)
    _log_activity(
        db,
        request.id,
        membership.user_id,
        payload.decision,
        f"Request {payload.decision}",
    )
    db.commit()

    request = _get_org_request(db, request_id, membership.organization_id)
    return RequestOut.model_validate(request)


@router.get("/{request_id}/activity", response_model=list[RequestActivityOut])
def get_activity(
    request_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> list[RequestActivityOut]:
    _get_org_request(db, request_id, membership.organization_id)  # scope check
    activity = db.scalars(
        select(RequestActivity)
        .where(RequestActivity.request_id == request_id)
        .order_by(RequestActivity.created_at.desc())
    )
    return [RequestActivityOut.model_validate(a) for a in activity]


@router.put("/{request_id}/document", status_code=status.HTTP_204_NO_CONTENT)
async def upload_document(
    request_id: uuid.UUID,
    file: UploadFile,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> None:
    """Attach (or replace) the original PDF for a request."""
    request = _get_org_request(db, request_id, membership.organization_id)

    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Please upload a PDF file"
        )
    content = await file.read()
    if len(content) > MAX_PDF_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="PDF exceeds the 10MB limit",
        )

    document = db.scalar(
        select(RequestDocument).where(RequestDocument.request_id == request.id)
    )
    if document is None:
        document = RequestDocument(request_id=request.id)
        db.add(document)
    document.filename = file.filename or "document.pdf"
    document.content_type = "application/pdf"
    document.content = content

    _log_activity(
        db,
        request.id,
        membership.user_id,
        "document_attached",
        f"Attached document {document.filename}",
    )
    db.commit()


@router.get("/{request_id}/document")
def get_document(
    request_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> Response:
    _get_org_request(db, request_id, membership.organization_id)  # scope check
    document = db.scalar(
        select(RequestDocument).where(RequestDocument.request_id == request_id)
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No document attached"
        )
    return Response(
        content=document.content,
        media_type=document.content_type,
        headers={"Content-Disposition": f'inline; filename="{document.filename}"'},
    )


@router.delete("/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_request(
    request_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> None:
    request = _get_org_request(db, request_id, membership.organization_id)
    db.delete(request)  # order lines, activity, document cascade
    db.commit()
