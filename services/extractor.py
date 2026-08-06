"""Извлечение структуры (услуги, цены, акции, репутация) со страниц конкурентов."""

import json

from openai import OpenAI

from config import settings

SYSTEM_PROMPT = """Ты аналитик рынка. Тебе дают текст страницы сайта компании,
которая разрабатывает ИИ-агентов, чат-ботов и автоматизацию.

Верни СТРОГО JSON такой формы:
{
  "services": [{"name": "...", "price": "... или null", "description": "... или null"}],
  "promotions": ["описание акции/скидки"],
  "rating": число или null,
  "reviews_count": целое или null,
  "portfolio_size": целое или null,
  "positioning": "1-2 предложения: на кого нацелены и чем себя выделяют, или null"
}

Правила:
- Бери только то, что реально есть в тексте. Ничего не выдумывай.
- В services — только услуги, которые компания продаёт клиентам. Не включай пункты меню,
  названия технологий, отрасли, статьи блога и разделы «о нас».
- Если одна и та же услуга названа по-разному, оставь один вариант.
- Название услуги должно быть самодостаточным: не «Стандарт», а «Разработка чат-ботов — тариф Стандарт».
  Так одна и та же позиция узнаётся при следующих проверках сайта.
- Цену пиши так, как она написана на сайте, вместе с валютой ("from $2,000", "від 15000 грн").
- Если цены нет — price: null.
- Пустые списки допустимы.
"""

_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)
    return _client


EMPTY: dict = {
    "services": [],
    "promotions": [],
    "rating": None,
    "reviews_count": None,
    "portfolio_size": None,
    "positioning": None,
}


def extract(page_text: str, url: str = "", known_services: list[str] | None = None) -> dict:
    """Прогоняет текст страницы через модель и возвращает нормализованный dict.

    known_services — названия услуг, уже известные по прошлым проверкам этого сайта.
    Без них модель каждый раз формулирует одно и то же по-новому, и повторный скан
    выглядит как появление новых услуг.
    """
    if not page_text.strip():
        return dict(EMPTY)

    known_block = ""
    if known_services:
        listed = "\n".join(f"- {name}" for name in known_services[:60])
        known_block = (
            "\n\nРанее у этой компании уже зафиксированы услуги ниже. Если позиция на странице — "
            "та же самая услуга, верни её ТОЧНО ТЕМ ЖЕ названием из списка. Новое название "
            "придумывай только для действительно новой услуги.\n"
            f"{listed}"
        )

    response = client().chat.completions.create(
        model=settings.deepseek_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT + known_block},
            {"role": "user", "content": f"URL: {url}\n\nТекст страницы:\n{page_text}"},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw = response.choices[0].message.content or "{}"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return dict(EMPTY)

    return normalize(data)


def normalize(data: dict) -> dict:
    result = dict(EMPTY)

    services = []
    for item in data.get("services") or []:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        services.append(
            {
                "name": name,
                "price": (item.get("price") or None),
                "description": (item.get("description") or None),
            }
        )
    result["services"] = services

    result["promotions"] = [
        p.strip() for p in (data.get("promotions") or []) if isinstance(p, str) and p.strip()
    ]

    for key, caster in (("rating", float), ("reviews_count", int), ("portfolio_size", int)):
        value = data.get(key)
        if value is None:
            continue
        try:
            result[key] = caster(value)
        except (TypeError, ValueError):
            result[key] = None

    positioning = data.get("positioning")
    result["positioning"] = positioning.strip() if isinstance(positioning, str) and positioning.strip() else None

    return result
