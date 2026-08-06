"""Оркестратор скана: страницы → извлечение → БД → изменения."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Competitor, ServiceOffering
from services import extractor, ingest, scraper


def scan_competitor(session: Session, competitor: Competitor, max_pages: int = 3) -> dict:
    known = list(
        session.scalars(
            select(ServiceOffering.name)
            .where(ServiceOffering.competitor_id == competitor.id)
            .order_by(ServiceOffering.first_seen_at)
        ).all()
    )
    snapshots = scraper.capture(session, competitor, max_pages=max_pages)
    results = [(s, extractor.extract(s.raw_text, s.url, known)) for s in snapshots]
    changes = ingest.apply_scan(session, competitor, results)

    return {
        "competitor": competitor.name,
        "domain": competitor.domain,
        "pages": [s.url for s in snapshots],
        "services": sum(len(data.get("services", [])) for _, data in results),
        "promotions": sum(len(data.get("promotions", [])) for _, data in results),
        "changes": changes,
    }


def scan_all(session: Session, include_own: bool = True, max_pages: int = 3) -> list[dict]:
    """Сканирует всех активных конкурентов (и наш сайт, если include_own)."""
    query = select(Competitor).where(Competitor.status == "active")
    if not include_own:
        query = query.where(Competitor.is_own.is_(False))

    reports = []
    for competitor in session.scalars(query).all():
        try:
            reports.append(scan_competitor(session, competitor, max_pages=max_pages))
        except Exception as exc:  # сайт может лежать или блокировать — не роняем весь прогон
            session.rollback()
            reports.append(
                {
                    "competitor": competitor.name,
                    "domain": competitor.domain,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return reports
