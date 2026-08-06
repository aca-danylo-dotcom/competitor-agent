"""Загрузка страниц конкурентов и сохранение текстовых снимков."""

import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from db.models import Competitor, PageSnapshot

REQUEST_TIMEOUT = 30
MAX_TEXT_CHARS = 20000

# Некоторые сайты режут «не браузерные» запросы — ходим с обычным User-Agent.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8,uk;q=0.7",
}

# Страницы с прайсом — главная цель обхода.
PRICE_PATTERNS = re.compile(
    r"^(pricing|price|prices|tariffs?|tarify|plans|packages|prajs|ceny|tseny|"
    r"цены|ціни|тарифы|прайс|стоимость|вартість|skolko-stoit)$",
    re.IGNORECASE,
)

# Профильные для нас услуги — ИИ-агенты, боты, автоматизация.
AI_PATTERNS = re.compile(
    r"(chatbot|chat-bot|chatbots|ai-agent|ai-agents|ai-development|automation|"
    r"botov|боты|чат-бот|ии-агент|ai-solutions)",
    re.IGNORECASE,
)

# Прочие страницы услуг — берём в последнюю очередь.
SERVICE_PATTERNS = re.compile(
    r"(services|solutions|razrabotka|uslugi|услуг|послуг)",
    re.IGNORECASE,
)


def _is_price_link(url: str, label: str) -> bool:
    """Прайс опознаём по последнему сегменту пути или короткой подписи ссылки.

    Так «/pricing» проходит, а «/ai-powered-dynamic-pricing-solution» — нет.
    """
    segment = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    if PRICE_PATTERNS.search(segment):
        return True
    return len(label) <= 20 and bool(PRICE_PATTERNS.search(label.strip()))

# Типовые адреса прайса: если на сайте нет ссылки, страница всё равно может существовать.
COMMON_PRICE_PATHS = ("/pricing", "/prices", "/price", "/plans", "/tariffs", "/ceny", "/tseny")


def fetch(url: str) -> tuple[str, str]:
    """Возвращает (финальный_url, html)."""
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return str(resp.url), resp.text


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text[:MAX_TEXT_CHARS]


def find_subpages(html: str, base_url: str, limit: int = 2) -> list[str]:
    """Ищет страницы прайса и услуг на том же домене; прайс идёт первым."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.lower()
    price_links: list[str] = []
    ai_links: list[str] = []
    service_links: list[str] = []
    seen: set[str] = {base_url.rstrip("/")}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = a.get_text(" ", strip=True)
        full = urljoin(base_url, href).split("#")[0].rstrip("/")
        if urlparse(full).netloc.lower() != base_host or full in seen:
            continue

        haystack = f"{href} {label}"
        if _is_price_link(full, label):
            bucket = price_links
        elif AI_PATTERNS.search(haystack):
            bucket = ai_links
        elif SERVICE_PATTERNS.search(haystack):
            bucket = service_links
        else:
            continue

        seen.add(full)
        bucket.append(full)

    # Прайса в ссылках нет — пробуем типовые адреса напрямую.
    if not price_links:
        base_text = html_to_text(html)
        root = f"{urlparse(base_url).scheme}://{base_host}"
        for path in COMMON_PRICE_PATHS:
            candidate = root + path
            if candidate in seen:
                continue
            try:
                resp = requests.get(candidate, headers=HEADERS, timeout=15)
            except requests.RequestException:
                continue
            if resp.status_code != 200:
                continue
            # SPA часто отдаёт 200 и ту же главную на любой путь — такое не берём.
            if html_to_text(resp.text) == base_text:
                continue
            price_links.append(candidate)
            break

    return (price_links + ai_links + service_links)[:limit]


def capture(session: Session, competitor: Competitor, max_pages: int = 3) -> list[PageSnapshot]:
    """Скачивает главную + найденные страницы прайса/услуг, сохраняет снимки."""
    final_url, html = fetch(competitor.url)
    pages = [(final_url, html)]

    for sub_url in find_subpages(html, final_url, limit=max_pages - 1):
        try:
            sub_final, sub_html = fetch(sub_url)
        except requests.RequestException:
            continue
        pages.append((sub_final, sub_html))

    snapshots: list[PageSnapshot] = []
    seen_texts: set[int] = set()
    for url, page_html in pages:
        text = html_to_text(page_html)
        if not text or hash(text) in seen_texts:
            continue
        seen_texts.add(hash(text))
        snapshot = PageSnapshot(competitor_id=competitor.id, url=url, raw_text=text)
        session.add(snapshot)
        snapshots.append(snapshot)

    session.flush()
    return snapshots
