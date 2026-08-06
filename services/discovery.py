"""Авто-поиск конкурентов через Tavily."""

import datetime as dt
from urllib.parse import urlparse

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from db.models import Competitor, DiscoveryRun, utcnow

TAVILY_URL = "https://api.tavily.com/search"
REQUEST_TIMEOUT = 30

# Каталоги, агрегаторы и медиа — это не конкуренты, а площадки, где конкуренты перечислены.
BLOCKED_DOMAINS = {
    "clutch.co",
    "goodfirms.co",
    "designrush.com",
    "upwork.com",
    "fiverr.com",
    "kwork.ru",
    "profi.ru",
    "fl.ru",
    "youdo.com",
    "freelancehunt.com",
    "kabanchik.ua",
    "freelance.ru",
    "weblancer.net",
    "toptal.com",
    "linkedin.com",
    "medium.com",
    "youtube.com",
    "reddit.com",
    "quora.com",
    "wikipedia.org",
    "github.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "instagram.com",
    "forbes.com",
    "g2.com",
    "producthunt.com",
}


class DiscoveryLimitError(RuntimeError):
    """Исчерпан дневной лимит ручных запусков поиска."""


def normalize_domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(":")[0]


def is_blocked(domain: str) -> bool:
    return any(domain == b or domain.endswith("." + b) for b in BLOCKED_DOMAINS)


def is_ignored_market(domain: str) -> bool:
    """Сайт с рынка, за которым мы не следим (см. IGNORED_DOMAIN_ZONES в .env)."""
    return any(domain.endswith(zone) for zone in settings.ignored_zone_list)


def manual_runs_today(session: Session) -> int:
    start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    runs = session.scalars(
        select(DiscoveryRun).where(DiscoveryRun.source == "manual")
    ).all()
    # started_at из SQLite приходит без tzinfo — сравниваем в наивном UTC.
    naive_start = start.replace(tzinfo=None)
    return sum(1 for r in runs if r.started_at.replace(tzinfo=None) >= naive_start)


def search(query: str, max_results: int = 10) -> list[dict]:
    resp = requests.post(
        TAVILY_URL,
        json={
            "api_key": settings.tavily_api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def run_discovery(
    session: Session,
    queries: list[str] | None = None,
    source: str = "manual",
    max_results: int = 10,
) -> dict:
    """Ищет конкурентов и складывает новых кандидатов в БД.

    Возвращает сводку: сколько всего найдено, сколько добавлено, какие домены пропущены.
    """
    if source == "manual" and manual_runs_today(session) >= settings.discovery_manual_daily_limit:
        raise DiscoveryLimitError(
            f"Дневной лимит ручных запусков исчерпан ({settings.discovery_manual_daily_limit})"
        )

    queries = queries or settings.discovery_query_list
    run = DiscoveryRun(source=source, queries=", ".join(queries))
    session.add(run)
    session.flush()

    known = {c.domain for c in session.scalars(select(Competitor)).all()}
    found = 0
    added: list[Competitor] = []
    skipped: list[str] = []

    for query in queries:
        for item in search(query, max_results=max_results):
            url = item.get("url", "")
            domain = normalize_domain(url)
            if not domain:
                continue
            found += 1
            if is_blocked(domain):
                skipped.append(f"{domain} (каталог)")
                continue
            if is_ignored_market(domain):
                skipped.append(f"{domain} (не наш рынок)")
                continue
            if domain in known:
                continue
            known.add(domain)
            competitor = Competitor(
                name=item.get("title") or domain,
                domain=domain,
                url=f"https://{domain}/",
                status="candidate",
            )
            session.add(competitor)
            added.append(competitor)

    run.found_count = found
    run.new_count = len(added)
    run.started_at = run.started_at or utcnow()
    session.commit()

    return {
        "queries": queries,
        "found": found,
        "added": [{"id": c.id, "name": c.name, "domain": c.domain} for c in added],
        "skipped": sorted(set(skipped)),
        "runs_today": manual_runs_today(session) if source == "manual" else None,
    }
