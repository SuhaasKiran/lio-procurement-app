"""Small, explainable keyword matching for the organisation's article catalog."""
import re
import unicodedata

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import Article, OrganizationMember

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {"a", "an", "and", "for", "from", "in", "of", "the", "to", "with"}
_ALIASES = {
    "notebook": "laptop",
    "notebooks": "laptop",
    "docking": "dock",
    "docks": "dock",
    "pcs": "piece",
    "ea": "piece",
}


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _tokens(value: str) -> list[str]:
    result: list[str] = []
    for token in _TOKEN.findall(_fold(value)):
        if token in _STOP_WORDS:
            continue
        if token.endswith("ing") and len(token) > 5:
            token = token[:-3]
        elif token.endswith("s") and not token.endswith("ss") and len(token) > 3:
            token = token[:-1]
        token = _ALIASES.get(token, token)
        if token and token not in result:
            result.append(token)
    return result


def _compact(value: str) -> str:
    return "".join(_TOKEN.findall(_fold(value)))


def _rank_articles(
    db: Session,
    membership: OrganizationMember,
    search: str,
) -> list[tuple[Article, str, float, list[str]]]:
    """Rank tenant-scoped results by exact id, exact text, then keywords."""
    query_tokens = _tokens(search)
    compact_query = _compact(search)
    if not compact_query or (not query_tokens and len(compact_query) < 3):
        return []

    filters = [Article.organization_id == membership.organization_id]
    if query_tokens:
        search_variants = set(query_tokens)
        search_variants.update(
            alias for alias, canonical in _ALIASES.items() if canonical in query_tokens
        )
        filters.append(
            or_(
                Article.article_number.ilike(f"%{search.strip()}%"),
                *(
                    Article.description.ilike(f"%{token}%")
                    for token in search_variants
                ),
            )
        )
    else:
        filters.append(Article.article_number.ilike(f"%{search.strip()}%"))

    candidates = list(
        db.scalars(
            select(Article)
            .where(*filters)
            .options(joinedload(Article.supplier))
        )
    )

    ranked: list[tuple[Article, str, float, list[str]]] = []
    for article in candidates:
        article_number = _compact(article.article_number)
        description = " ".join(_tokens(article.description))
        description_tokens = set(description.split())
        shared = [token for token in query_tokens if token in description_tokens]

        if article_number == compact_query:
            match_type, score, matched = "article_number", 1.0, [article.article_number]
        elif description == " ".join(query_tokens):
            match_type, score, matched = "exact_description", 0.99, shared
        elif shared:
            query_coverage = len(shared) / max(1, len(query_tokens))
            item_coverage = len(shared) / max(1, len(description_tokens))
            score = round(0.7 * query_coverage + 0.3 * item_coverage, 3)
            match_type, matched = "keywords", shared
        elif article_number and compact_query in article_number:
            match_type, score, matched = "article_number", 0.9, [article.article_number]
        else:
            continue
        ranked.append((article, match_type, score, matched))

    ranked.sort(
        key=lambda match: (
            -match[2],
            match[0].article_number.casefold(),
        )
    )
    return ranked


def suggest_articles(
    db: Session,
    membership: OrganizationMember,
    search: str,
    limit: int = 8,
) -> list[tuple[Article, str, float, list[str]]]:
    """Return the first page of catalog suggestions for existing callers."""
    return _rank_articles(db, membership, search)[:limit]


def suggest_article_page(
    db: Session,
    membership: OrganizationMember,
    search: str,
    limit: int = 8,
    offset: int = 0,
) -> tuple[list[tuple[Article, str, float, list[str]]], int]:
    """Return one page of ranked suggestions plus the total number of matches."""
    ranked = _rank_articles(db, membership, search)
    return ranked[offset : offset + limit], len(ranked)
