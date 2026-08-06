"""Отбор найденных сайтов: похожа ли компания на нас.

Поиск возвращает вперемешку агентства, статьи-подборки, каталоги и SaaS-платформы.
Раньше разбирал человек; теперь сайт открывает модель и отвечает, чем он является
и похож ли на нас. Образец берётся с нашего собственного сайта — так «похожие»
определяются нашим делом, а не списком ключевых слов, который быстро устареет.
"""

import json
import logging

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from db.models import Competitor, ServiceOffering, utcnow
from services import scraper
from services.extractor import client

log = logging.getLogger(__name__)

# Что нас интересует: компания, которая продаёт ту же работу, что и мы.
SYSTEM_PROMPT = """Ты отбираешь компании, похожие на наше агентство.

Тебе дают: чем занимаемся мы, и текст главной страницы найденного сайта.

Верни СТРОГО JSON:
{
  "kind": "agency | freelancer | platform | article | directory | other",
  "similar": true | false,
  "reason": "одно предложение: чем это на самом деле и почему похоже или нет"
}

Что считать похожим (similar: true):
- компания или студия, которая делает работу на заказ из той же области, что и мы:
  ИИ-агенты, чат-боты, автоматизация процессов, интеграции, CRM и панели, сайты;
- размер и страна значения не имеют, важно совпадение занятия.

Что похожим НЕ считать (similar: false):
- статьи и подборки «Топ-10 компаний», блоги, новости — это kind: "article";
- каталоги и рейтинги подрядчиков — "directory";
- биржи фриланса и маркетплейсы услуг — "platform";
- SaaS-конструкторы, где клиент собирает бота сам, — тоже "platform";
- сайты из совсем другой области — "other".

Если текст пустой или ничего не понятно — similar: false, reason честно об этом.
"""

DEFAULT_VERDICT = {"kind": "other", "similar": False, "reason": "Страница не открылась"}


def our_profile(session: Session) -> str:
    """Чем занимаемся мы — по нашему же сайту."""
    own = session.scalars(select(Competitor).where(Competitor.is_own.is_(True))).first()
    if own is None:
        return (
            "Агентство разработки ИИ-агентов, чат-ботов, автоматизации бизнес-процессов, "
            "CRM-панелей и сайтов."
        )

    services = session.scalars(
        select(ServiceOffering.name).where(ServiceOffering.competitor_id == own.id)
    ).all()
    listed = ", ".join(services[:20]) if services else "ИИ-агенты, боты, автоматизация"
    return f"{own.name} ({own.domain}). Услуги: {listed}."


def judge(page_text: str, profile: str) -> dict:
    """Решает, похож ли сайт на нас. Ошибку наружу не пускает — просто «не похож»."""
    if not page_text.strip():
        return dict(DEFAULT_VERDICT)

    try:
        response = client().chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Мы: {profile}\n\nТекст найденного сайта:\n{page_text[:6000]}",
                },
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        data = json.loads(response.choices[0].message.content or "{}")
    except Exception:  # сеть, лимит, кривой ответ — сайт просто не проходит отбор
        log.exception("не удалось оценить сайт")
        return {"kind": "other", "similar": False, "reason": "Оценить сайт не удалось"}

    return {
        "kind": str(data.get("kind") or "other"),
        "similar": bool(data.get("similar")),
        "reason": str(data.get("reason") or "").strip() or "Без объяснения",
    }


def screen(session: Session, competitor: Competitor, profile: str) -> dict:
    """Открывает сайт, судит его и сразу проставляет статус."""
    try:
        _, html = scraper.fetch(competitor.url)
        text = scraper.html_to_text(html)
    except requests.RequestException as exc:
        verdict = {"kind": "other", "similar": False, "reason": f"Сайт не открылся: {exc.__class__.__name__}"}
    else:
        verdict = judge(text, profile)

    competitor.kind = verdict["kind"]
    competitor.screening_note = verdict["reason"]
    competitor.status = "active" if verdict["similar"] else "rejected"
    if verdict["similar"] and competitor.approved_at is None:
        competitor.approved_at = utcnow()
    return verdict


def screen_pending(session: Session, profile: str | None = None) -> list[dict]:
    """Прогоняет отбор по всем сайтам, которые ещё не оценивались."""
    profile = profile or our_profile(session)
    results = []

    for competitor in session.scalars(
        select(Competitor).where(Competitor.status == "candidate")
    ).all():
        verdict = screen(session, competitor, profile)
        results.append({"domain": competitor.domain, **verdict})
        session.commit()

    return results
