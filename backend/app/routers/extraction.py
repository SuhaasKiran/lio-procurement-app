from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.deps import get_current_membership, get_current_user
from app.db.session import get_db
from app.models import Organization, OrganizationMember
from app.schemas.extraction import (
    CategoryAssignmentRequest,
    CategoryAssignmentResponse,
    ExtractionRequest,
    ExtractionResponse,
)
from app.services.extraction import classify_order_lines, extract_vendor_data

router = APIRouter(prefix="/extraction", tags=["extraction"])


@router.post("", response_model=ExtractionResponse, response_model_by_alias=True,
             dependencies=[Depends(get_current_user)])
def extract(payload: ExtractionRequest, db: Session = Depends(get_db)) -> ExtractionResponse:
    """Extract structured vendor data from PDF text via OpenAI.

    Returns ``{success, data?, error?, missingFields?}`` — the same envelope the
    original server action produced (errors are part of the payload, not HTTP
    errors, so the UI can show partial-extraction warnings).
    """
    return extract_vendor_data(payload.text, db)


@router.post(
    "/classify-lines",
    response_model=CategoryAssignmentResponse,
    response_model_by_alias=True,
)
def classify_lines(
    payload: CategoryAssignmentRequest,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> CategoryAssignmentResponse:
    """Classify each line using only the authenticated user's organization rules."""
    organization = db.get(Organization, membership.organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return CategoryAssignmentResponse(
        lines=classify_order_lines(payload.lines, organization, db)
    )
