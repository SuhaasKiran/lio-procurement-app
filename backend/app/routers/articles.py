import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.core.deps import get_current_membership
from app.db.session import get_db
from app.models import Article, OrganizationMember
from app.schemas.articles import ArticleMatchOut, ArticleMatchPage, ArticleOut, ArticlePage
from app.services.article_matching import suggest_article_page

router = APIRouter(prefix="/articles", tags=["articles"])
# Uvicorn's configured console logger makes these operational messages visible
# in the backend terminal and container logs at INFO level.
logger = logging.getLogger("uvicorn.error")


@router.get("/suggestions", response_model=ArticleMatchPage)
def article_suggestions(
    search: str = Query(min_length=2, max_length=1000),
    limit: int = Query(default=8, ge=1, le=20),
    offset: int = Query(default=0, ge=0),
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> ArticleMatchPage:
    """Suggest catalog items using exact ids, exact text, then keywords."""
    organization_id = membership.organization_id
    logger.info(
        "Catalog search: status=started organization_id=%s query_length=%d",
        organization_id,
        len(search),
    )
    try:
        matches, total = suggest_article_page(db, membership, search, limit, offset)
        results = [
            ArticleMatchOut(
                id=article.id,
                article_number=article.article_number,
                supplier_id=article.supplier_id,
                supplier_name=article.supplier.name if article.supplier else None,
                description=article.description,
                unit_price=article.unit_price,
                currency=article.currency,
                unit=article.unit,
                quantity=article.quantity,
                created_at=article.created_at,
                match_type=match_type,
                score=score,
                matched_terms=matched_terms,
            )
            for article, match_type, score, matched_terms in matches
        ]
    except Exception:
        logger.exception(
            "Catalog search: status=failed organization_id=%s",
            organization_id,
        )
        raise

    logger.info(
        "Catalog search: status=completed organization_id=%s matching_items=%d total_matches=%d",
        organization_id,
        len(results),
        total,
    )
    return ArticleMatchPage(items=results, total=total, limit=limit, offset=offset)


@router.get("", response_model=ArticlePage)
def list_articles(
    search: str | None = Query(
        default=None, description="Filter by description or article number (case-insensitive)"
    ),
    supplier_id: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> ArticlePage:
    filters = [Article.organization_id == membership.organization_id]
    if search:
        term = f"%{search}%"
        filters.append(
            or_(Article.description.ilike(term), Article.article_number.ilike(term))
        )
    if supplier_id:
        filters.append(Article.supplier_id == supplier_id)

    total = db.scalar(
        select(func.count()).select_from(Article).where(*filters)
    ) or 0

    stmt = (
        select(Article)
        .where(*filters)
        .options(joinedload(Article.supplier))
        .order_by(Article.article_number)
        .offset(offset)
        .limit(limit)
    )
    items = [
        ArticleOut(
            id=a.id,
            article_number=a.article_number,
            supplier_id=a.supplier_id,
            supplier_name=a.supplier.name if a.supplier else None,
            description=a.description,
            unit_price=a.unit_price,
            currency=a.currency,
            unit=a.unit,
            quantity=a.quantity,
            created_at=a.created_at,
        )
        for a in db.scalars(stmt)
    ]
    return ArticlePage(items=items, total=total, limit=limit, offset=offset)


@router.get("/count")
def count_articles(
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    total = (
        db.scalar(
            select(func.count())
            .select_from(Article)
            .where(Article.organization_id == membership.organization_id)
        )
        or 0
    )
    return {"count": total}
