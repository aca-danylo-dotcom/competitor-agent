"""Загрузка страниц конкурентов и сохранение текстовых снимков."""

import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from config import settings
from db.models import Competitor, PageSnapshot

REQUEST_TIMEOUT = 30
# Пробных запросов на сайт много, поэтому ждём каждый заметно меньше.
PROBE_TIMEOUT = 12
# И сколько всего времени готовы потратить на поиск цен у одного сайта.
PROBE_TIME_BUDGET = 45.0
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

# Где ещё держат цены: карточки продуктов, наборы, курсы, магазин.
# Такие адреса под «pricing» не подходят — untaylored.com держит цену на
# /business-builder-bundle, и по одним только словам «price/services» её не найти.
OFFER_PATTERNS = re.compile(
    r"(bundle|package|product|shop|store|buy|order|checkout|course|kit|template|"
    r"membership|subscribe|offer|deal|tovar|kupit)",
    re.IGNORECASE,
)

# Страницы, которые смотреть незачем. Кроме заведомо пустых разделов сюда
# входят две ловушки: статьи блога, где стоят цены чужих компаний, и служебные
# страницы оплаты, где сумма нулевая или чья-то чужая.
SKIP_PATTERNS = re.compile(
    r"(/blog|/post|/news|/article|/careers|/jobs|/vacan|/privacy|/terms|/policy|/cookie|"
    r"/about|/team|/contact|/login|/signin|/signup|/cart|/faq|/checkout|/paypal|"
    r"/order-|/thank|/thanks|/confirmation|/success|/invoice|/receipt|/account|"
    r"\.pdf$|\.jpg$|\.png$)",
    re.IGNORECASE,
)

# Признак того, что на странице есть деньги: символ валюты или код рядом с числом.
# Символ валюты встречается и перед числом («$49»), и после него («25 000 ₴»).
PRICE_IN_TEXT = re.compile(
    r"(?:[$€£₴₽]\s?(\d[\d\s.,]*)|(\d[\d\s.,]*)\s?(?:USD|EUR|GBP|UAH|PLN|грн|[$€£₴₽]))",
    re.IGNORECASE,
)


def has_price(text: str) -> bool:
    """Есть ли на странице настоящая цена.

    Нули не в счёт: на страницах оплаты и в пустых корзинах стоит «$0.00», и
    без этой проверки такая страница выглядела бы прайсом.
    """
    for match in PRICE_IN_TEXT.finditer(text):
        raw = (match.group(1) or match.group(2) or "").replace(" ", "").replace(",", "")
        try:
            if float(raw.rstrip(".")) >= 1:
                return True
        except ValueError:
            continue
    return False


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


def fetch(url: str, timeout: int = REQUEST_TIMEOUT) -> tuple[str, str]:
    """Возвращает (финальный_url, html)."""
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
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


def candidates(html: str, base_url: str) -> list[str]:
    """Внутренние адреса, где может лежать цена, — по убыванию надежды.

    Порядок важен: прощупываем ограниченное число страниц, и первыми должны идти
    те, где цена вероятнее всего. Явные страницы прайса, потом карточки товаров
    и наборов, потом профильные услуги, потом всё остальное.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.lower()
    price_links: list[str] = []
    offer_links: list[str] = []
    ai_links: list[str] = []
    service_links: list[str] = []
    seen: set[str] = {base_url.rstrip("/")}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = a.get_text(" ", strip=True)
        full = urljoin(base_url, href).split("#")[0].rstrip("/")
        if urlparse(full).netloc.lower() != base_host or full in seen:
            continue
        if SKIP_PATTERNS.search(full):
            continue

        haystack = f"{href} {label}"
        if _is_price_link(full, label):
            bucket = price_links
        elif OFFER_PATTERNS.search(haystack):
            bucket = offer_links
        elif AI_PATTERNS.search(haystack):
            bucket = ai_links
        elif SERVICE_PATTERNS.search(haystack):
            bucket = service_links
        else:
            continue

        seen.add(full)
        bucket.append(full)

    # Типовые адреса прайса на случай, если ссылки на него в меню нет.
    root = f"{urlparse(base_url).scheme}://{base_host}"
    guesses = [root + path for path in COMMON_PRICE_PATHS if root + path not in seen]

    return price_links + guesses + offer_links + ai_links + service_links


def find_subpages(html: str, base_url: str, limit: int = 2) -> list[str]:
    """Совместимый со старым кодом отбор: первые кандидаты без прощупывания."""
    return candidates(html, base_url)[:limit]


def sitemap_urls(base_url: str, limit: int = 500) -> list[str]:
    """Адреса из карты сайта — список страниц, который сайт публикует сам.

    Это надёжнее ссылок с главной: в меню выносят не всё, а карточка услуги с
    ценой может быть доступна только из каталога или вообще из поиска.
    """
    root = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
    found: list[str] = []
    queue = [root + "/sitemap.xml", root + "/sitemap_index.xml"]
    seen_maps: set[str] = set()

    while queue and len(found) < limit:
        target = queue.pop(0)
        if target in seen_maps:
            continue
        seen_maps.add(target)
        try:
            resp = requests.get(target, headers=HEADERS, timeout=PROBE_TIMEOUT)
        except requests.RequestException:
            continue
        if resp.status_code != 200 or "<" not in resp.text[:200]:
            continue

        locations = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text, re.IGNORECASE)
        for loc in locations:
            # Карта может ссылаться на другие карты — их тоже разбираем.
            if loc.endswith(".xml") and len(seen_maps) < 12:
                queue.append(loc)
            elif urlparse(loc).netloc == urlparse(base_url).netloc:
                found.append(loc.split("#")[0].rstrip("/"))

    return found[:limit]


def _rank(url: str) -> int:
    """Чем раньше открыть страницу. Меньше — важнее."""
    if _is_price_link(url, ""):
        return 0
    if OFFER_PATTERNS.search(url):
        return 1
    if AI_PATTERNS.search(url):
        return 2
    if SERVICE_PATTERNS.search(url):
        return 3
    return 4


def crawl(
    base_url: str,
    html: str,
    page_limit: int | None = None,
    time_budget: float | None = None,
) -> list[tuple[str, str]]:
    """Обходит сайт и возвращает все страницы, на которых есть цены.

    Порядок обхода — от вероятного к остальному, но перебираются в итоге все
    страницы, до которых успеваем дотянуться. Проверка «есть ли деньги» идёт
    простым поиском по тексту: скачать страницу дёшево, спросить модель — нет.
    """
    page_limit = page_limit or settings.crawl_page_limit
    time_budget = time_budget or settings.crawl_time_budget

    base_text = html_to_text(html)
    base_host = urlparse(base_url).netloc.lower()

    urls = candidates(html, base_url)
    known = set(urls) | {base_url.rstrip("/")}
    for url in sitemap_urls(base_url):
        if url not in known and not SKIP_PATTERNS.search(url):
            known.add(url)
            urls.append(url)

    urls.sort(key=_rank)

    found: list[tuple[str, str]] = []
    visited = 0
    deadline = time.monotonic() + time_budget

    for url in urls:
        if visited >= page_limit or time.monotonic() > deadline:
            break
        if urlparse(url).netloc.lower() != base_host:
            continue
        visited += 1
        try:
            # Страниц много, и каждая может уйти в долгое ожидание. Медленную
            # дешевле бросить: цена, скорее всего, найдётся на другой.
            final_url, page_html = fetch(url, timeout=PROBE_TIMEOUT)
        except requests.RequestException:
            continue
        text = html_to_text(page_html)
        # SPA отдаёт главную на любой путь — такую страницу за находку не считаем.
        if not text or text == base_text:
            continue
        if has_price(text):
            found.append((final_url, page_html))

    return found


def probe(urls: list[str], base_text: str, want: int, budget: int) -> list[tuple[str, str]]:
    """Старый точечный вариант обхода — оставлен для тестов и разовых проверок."""
    found: list[tuple[str, str]] = []
    for url in urls[:budget]:
        if len(found) >= want:
            break
        try:
            final_url, html = fetch(url, timeout=PROBE_TIMEOUT)
        except requests.RequestException:
            continue
        text = html_to_text(html)
        if text and text != base_text and has_price(text):
            found.append((final_url, html))
    return found


def capture(session: Session, competitor: Competitor, max_pages: int = 3) -> list[PageSnapshot]:
    """Обходит сайт и сохраняет главную вместе со всеми страницами, где есть цены.

    max_pages задаёт, сколько страниц брать, если цен не нашлось нигде: тогда
    смотрим профильные разделы, чтобы собрать хотя бы состав услуг.
    """
    final_url, html = fetch(competitor.url)
    pages = [(final_url, html)]

    priced = crawl(final_url, html)
    pages.extend(priced[: settings.price_pages_limit])

    if not priced:
        taken = {url for url, _ in pages}
        for url in candidates(html, final_url):
            if len(pages) >= max_pages:
                break
            if url in taken:
                continue
            try:
                sub_final, sub_html = fetch(url, timeout=PROBE_TIMEOUT)
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
