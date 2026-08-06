"""Сводка цен по рынку и рекомендация собственного прайса.

На нашем сайте цен нет, поэтому опорой служат цены конкурентов: собираем актуальные
записи из price_entries, приводим к числам и просим модель предложить наш прайс.
"""

import json
import re
import statistics

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from db.models import Competitor, PriceEntry, ServiceOffering
from services.extractor import client

CURRENCY_SIGNS = {
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "₴": "UAH",
    "грн": "UAH",
    "uah": "UAH",
    "₽": "RUB",
    "руб": "RUB",
    "rub": "RUB",
    "zł": "PLN",
    "pln": "PLN",
}

# Цена «в месяц» и «за час» несравнима с ценой за проект — помечаем период.
PERIOD_PATTERNS = (
    ("month", r"/мес|в мес|мес\.|per month|/mo\b|monthly|на місяц"),
    ("hour", r"/час|в час|per hour|/hr\b|hourly|за годину"),
    ("project", r"за проект|per project|/project"),
)

NUMBER_RE = re.compile(r"\d[\d\s.,]*")


def detect_currency(text: str) -> str | None:
    low = text.lower()
    for token, code in CURRENCY_SIGNS.items():
        if token in low:
            return code
    return None


def parse_numbers(text: str) -> list[float]:
    """Вытаскивает денежные значения: '$1,500 - $3,000' → [1500.0, 3000.0]."""
    values: list[float] = []
    for match in NUMBER_RE.finditer(text):
        chunk = match.group().strip().rstrip(".,")
        cleaned = chunk.replace(" ", "").replace(" ", "")
        # Точка/запятая с 1-2 цифрами после — десятичный разделитель, иначе разряды.
        cleaned = re.sub(r"[.,](?=\d{3}\b)", "", cleaned)
        cleaned = cleaned.replace(",", ".")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        if value >= 1:
            values.append(value)
    return values


def detect_period(text: str) -> str:
    low = text.lower()
    for period, pattern in PERIOD_PATTERNS:
        if re.search(pattern, low):
            return period
    return "one_time"


def parse_price(text: str) -> dict | None:
    """'from $2,000' → {'currency': 'USD', 'low': 2000.0, 'high': 2000.0, 'period': ...}"""
    numbers = parse_numbers(text)
    if not numbers:
        return None
    return {
        "currency": detect_currency(text),
        "low": min(numbers),
        "high": max(numbers),
        "period": detect_period(text),
        "text": text,
    }


def market_prices(session: Session) -> list[dict]:
    """Актуальная цена по каждой услуге каждого активного конкурента (кроме нас)."""
    rows = session.execute(
        select(Competitor, ServiceOffering, PriceEntry)
        .join(ServiceOffering, ServiceOffering.competitor_id == Competitor.id)
        .join(PriceEntry, PriceEntry.service_offering_id == ServiceOffering.id)
        .where(Competitor.is_own.is_(False), Competitor.status == "active")
        .order_by(PriceEntry.captured_at.asc())
    ).all()

    latest: dict[int, dict] = {}
    for competitor, offering, entry in rows:
        parsed = parse_price(entry.price_text)
        if parsed is None:
            continue
        latest[offering.id] = {
            "competitor": competitor.name,
            "domain": competitor.domain,
            "service": offering.name,
            "price_text": entry.price_text,
            **parsed,
        }
    return list(latest.values())


def summary(session: Session) -> dict:
    """Статистика рынка по валютам."""
    prices = market_prices(session)
    by_currency: dict[str, list[dict]] = {}
    for item in prices:
        period = "" if item["period"] == "one_time" else f" / {item['period']}"
        by_currency.setdefault(f"{item['currency'] or '?'}{period}", []).append(item)

    stats = {}
    for currency, items in by_currency.items():
        lows = [i["low"] for i in items]
        highs = [i["high"] for i in items]
        stats[currency] = {
            "count": len(items),
            "min": min(lows),
            "median_low": statistics.median(lows),
            "median_high": statistics.median(highs),
            "max": max(highs),
        }

    return {"prices": prices, "stats": stats}


def own_services(session: Session) -> list[dict]:
    rows = session.execute(
        select(Competitor, ServiceOffering)
        .join(ServiceOffering, ServiceOffering.competitor_id == Competitor.id)
        .where(Competitor.is_own.is_(True))
    ).all()
    return [{"name": s.name, "description": s.description} for _, s in rows]


RECOMMEND_PROMPT = """Ты консультант по ценообразованию для агентства разработки ИИ-агентов и чат-ботов.

Тебе дают:
1) услуги нашего агентства (цен на сайте пока нет);
2) цены конкурентов, собранные с их сайтов;
3) статистику рынка.

Предложи прайс для нашего агентства. Верни СТРОГО JSON:
{
  "recommendations": [
    {"service": "наша услуга", "price": "предлагаемая цена с валютой",
     "rationale": "почему такая цена — со ссылкой на конкретных конкурентов",
     "market_position": "below | at | above"}
  ],
  "notes": "1-3 предложения: общий вывод по позиционированию и рискам"
}

Правила:
- Опирайся только на переданные цены, не выдумывай чужие.
- Если по услуге рыночных данных мало — скажи об этом в rationale.
- Валюту бери ту, в которой считает рынок в этих данных.
"""


def recommend(session: Session) -> dict:
    """Просит модель предложить наш прайс на основе собранных цен конкурентов."""
    data = summary(session)
    if not data["prices"]:
        return {
            "recommendations": [],
            "notes": "Нет собранных цен конкурентов — сначала нужно просканировать активных конкурентов.",
            "market": data["stats"],
        }

    ours = own_services(session)
    payload = {
        "our_services": ours,
        "competitor_prices": [
            {
                "competitor": p["competitor"],
                "service": p["service"],
                "price": p["price_text"],
                "currency": p["currency"],
            }
            for p in data["prices"]
        ],
        "market_stats": data["stats"],
    }

    response = client().chat.completions.create(
        model=settings.deepseek_model,
        messages=[
            {"role": "system", "content": RECOMMEND_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    try:
        result = json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        result = {"recommendations": [], "notes": "Модель вернула некорректный JSON."}

    result["market"] = data["stats"]
    return result
